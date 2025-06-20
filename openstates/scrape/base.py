import boto3  # noqa
import datetime
import importlib
import json
import jsonschema
import logging
import os
import scrapelib
import subprocess
import time
import uuid
from collections import defaultdict, OrderedDict
from elasticsearch import Elasticsearch
from elasticsearch.exceptions import ConnectionError as ESConnectionError
from jsonschema import Draft3Validator, FormatChecker
from warnings import filterwarnings
from .. import utils, settings
from ..exceptions import ScrapeError, ScrapeValueError, EmptyScrape


def replace_none_in_dict(bill_json: dict) -> dict:
    """Recursively convert None values to empty strings in a dictionary"""
    if isinstance(bill_json, dict):
        return {key: replace_none_in_dict(value) for key, value in bill_json.items()}
    elif isinstance(bill_json, list):
        return [replace_none_in_dict(item) for item in bill_json]
    elif bill_json is None:
        return ""
    elif isinstance(bill_json, datetime.date):
        return bill_json.strftime('%Y-%m-%d')
    else:
        return bill_json


@FormatChecker.cls_checks("uri-blank")
def uri_blank(value):
    return value == '' or FormatChecker().conforms(value, 'uri')


@FormatChecker.cls_checks('uri')
def check_uri(val):
    return val and val.startswith(('http://', 'https://', 'ftp://'))


def cleanup_list(obj, default):
    if not obj:
        obj = default
    elif isinstance(obj, str):
        obj = [obj]
    elif not isinstance(obj, list):
        obj = list(obj)
    return sorted(obj)


def clean_whitespace(obj):
    '''deep whitespace clean for ScrapeObj & dicts'''
    if isinstance(obj, dict):
        items = obj.items()
        use_setattr = False
    elif isinstance(obj, object):
        items = obj.__dict__.items()
        use_setattr = True

    for k, v in items:
        if isinstance(v, str) and v:
            newv = v.strip()
        elif isinstance(v, list) and v:
            if not v:
                continue
            elif isinstance(v[0], str):
                newv = [i.strip() for i in v]
            elif isinstance(v[0], (dict, object)):
                newv = [clean_whitespace(i) for i in v]
            else:
                raise ValueError(f'Unhandled case, {k} is list of {type(v[0])}')
        else:
            continue

        if use_setattr:
            setattr(obj, k, newv)
        else:
            obj[k] = newv

    return obj


class Scraper(scrapelib.Scraper):
    '''Base class for all scrapers'''

    def __init__(
        self,
        jurisdiction,
        datadir,
        *,
        strict_validation=True,
        fastmode=False,
        realtime=False,
        kafka=None,
        kafka_producer=None,
        file_archiving_enabled=False,
    ):
        super(Scraper, self).__init__()

        # set options
        self.jurisdiction = jurisdiction
        self.datadir = datadir
        self.realtime = realtime
        self.kafka = kafka
        self.kafka_producer = kafka_producer
        self.file_archiving_enabled = file_archiving_enabled

        # scrapelib setup
        self.timeout = settings.SCRAPELIB_TIMEOUT
        self.requests_per_minute = settings.SCRAPELIB_RPM
        self.retry_attempts = settings.SCRAPELIB_RETRY_ATTEMPTS
        self.retry_wait_seconds = settings.SCRAPELIB_RETRY_WAIT_SECONDS
        self.verify = settings.SCRAPELIB_VERIFY

        # output
        self.output_file_path = None

        if fastmode:
            self.requests_per_minute = 0
            self.cache_write_only = False
            self.es_client = self.init_elasticsearch_client()

        self.existing_session_bills = None

        # validation
        self.strict_validation = strict_validation

        # 'type' -> {set of names}
        self.output_names = defaultdict(set)

        # logging convenience methods
        self.logger = logging.getLogger('openstates')
        self.info = self.logger.info
        self.debug = self.logger.debug
        self.warning = self.logger.warning
        self.error = self.logger.error
        self.critical = self.logger.critical

        # caching
        if settings.CACHE_DIR:
            print(settings.CACHE_BUCKET+'/'+self.jurisdiction.name)
            if os.environ.get('SYNC_S3_ARCHIVE', 'false').lower() == 'true':
                self.info(f"Syncing cache from S3 bucket {settings.CACHE_BUCKET}")
                os.makedirs(settings.CACHE_DIR, exist_ok=True)
                subprocess.run(['aws', 's3', 'sync', settings.CACHE_BUCKET+'/'+self.jurisdiction.name , settings.CACHE_DIR], check=True)
                self.info("Cache sync completed")
                self.cache_storage = scrapelib.FileCache(settings.CACHE_DIR)

        modname = os.environ.get('SCRAPE_OUTPUT_HANDLER')
        if modname is None:
            self.scrape_output_handler = None
        else:
            handler = importlib.import_module(modname)
            self.scrape_output_handler = handler.Handler(self)

    def push_to_queue(self):
        '''Push this output to the sqs for realtime imports.'''

        # Create SQS client
        sqs = boto3.client('sqs')

        queue_url = settings.SQS_QUEUE_URL
        bucket = settings.S3_REALTIME_BASE.replace('s3://', '')

        message_body = json.dumps(
            {
                'file_path': self.output_file_path,
                'bucket': bucket,
                'jurisdiction_id': self.jurisdiction.jurisdiction_id,
                'jurisdiction_name': self.jurisdiction.name,
                'file_archiving_enabled': self.file_archiving_enabled,
            }
        )

        # Send message to SQS queue
        response = sqs.send_message(
            QueueUrl=queue_url,
            DelaySeconds=10,
            MessageAttributes={
                'Title': {'DataType': 'String', 'StringValue': 'S3 Output Path'},
                'Author': {'DataType': 'String', 'StringValue': 'Open States'},
            },
            MessageBody=message_body,
        )
        self.info(f"Message ID: {response['MessageId']}")

    def init_elasticsearch_client(self):
        """
        Initialize the Elasticsearch client.
        """
        es_cloud_id = os.environ.get(
            "ELASTIC_CLOUD_ID",
            None,
        )
        es_user = os.environ.get("ELASTIC_BASIC_AUTH_USER", None)
        es_password = os.environ.get("ELASTIC_BASIC_AUTH_PASS", None)

        if not any([es_cloud_id, es_user, es_password]):
            raise ScrapeError(
                "Elasticsearch credentials are not set. "
                "Please set ELASTIC_CLOUD_ID, ELASTIC_BASIC_AUTH_USER, and ELASTIC_BASIC_AUTH_PASS."
            )
        filterwarnings("ignore", category=Warning, module="elasticsearch")

        es_client = Elasticsearch(
            cloud_id=es_cloud_id,
            http_auth=(
                es_user,
                es_password,
            ),  # http_auth is used in ES 7.x instead of basic_auth (to match python 3.9 limits)
            verify_certs=True,
        )
        return es_client

    def get_elastic_entries(self, bill_json: dict, jurisdiction: str) -> dict:
        """
        Check if the bill exists in Elasticsearch and return all matching entries.
        """
        session = bill_json.get("legislative_session")
        try:
            must_clauses = []
            if jurisdiction:
                must_clauses.append({"term": {"jurisdiction.keyword": jurisdiction}})
            if session:
                must_clauses.append({"term": {"legislative_session.keyword": session}})

            query = {
                "query": {"bool": {"must": must_clauses}},
                "size": 10000,  
            }
            
            response = self.es_client.search(
                index="cyclades",
                body=query,
                scroll='2m'
            )
            
            all_hits = {}
            scroll_id = response['_scroll_id']
            
            for hit in response["hits"]["hits"]:
                all_hits[hit["_source"]["identifier"]] = hit["_source"]
            
            while len(response['hits']['hits']) > 0:
                response = self.es_client.scroll(scroll_id=scroll_id, scroll='2m')
                
                for hit in response["hits"]["hits"]:
                    all_hits[hit["_source"]["identifier"]] = hit["_source"]
            
            self.es_client.clear_scroll(scroll_id=scroll_id)
            
            return all_hits if all_hits else None
        except ESConnectionError as e:
            print(f"Connection error with Elasticsearch. Verify that the credentials are correct and updated: {e}")
            return None
        except Exception as e:
            print(f"Error during Elasticsearch scroll: {e}")
            return None

    def save_object(self, obj):
        '''
        Save object to disk as JSON.

        Generally shouldn't be called directly.
        '''
        clean_whitespace(obj)
        obj.pre_save(self.jurisdiction)

        filename = f'{obj._type}_{obj._id}.json'.replace('/', '-')
        self.info(f'save {obj._type} {obj} as {filename}')

        self.debug(
            json.dumps(
                OrderedDict(sorted(obj.as_dict().items())),
                cls=utils.JSONEncoderPlus,
                indent=4,
                separators=(',', ': '),
            )
        )

        self.output_names[obj._type].add(filename)

        if self.scrape_output_handler is None:
            file_path = os.path.join(self.datadir, filename)

            try:
                # Remove redundant prefix and amend file path
                upload_file_path = file_path[
                    file_path.index('_data') + len('_data') + 1 :
                ]
                jurisdiction = upload_file_path[:2]
                # Vote events will be routed through this conditional
                if hasattr(obj, 'motion_text'):
                    identifier = obj.bill_identifier
                    logging.info(
                        f'Saving vote event from bill {identifier}.'
                    )
                # Bills will be routed through this conditional
                elif hasattr(obj, 'legislative_session') and obj.legislative_session:
                    session = obj.legislative_session
                    identifier = obj.identifier
                    upload_file_path = (
                        f'{jurisdiction}/{session}/{identifier}/{upload_file_path[3:]}'
                    )
                # All other ancillary JSONs will be routed here (e.g. jurisdiction JSONs)
                else:
                    upload_file_path = f'{jurisdiction}/{"Jurisdiction_Information"}/{upload_file_path[3:]}'

            except ValueError:
                upload_file_path = file_path

            # Fastmode S3 cache check
            if (
                self.requests_per_minute == 0 and  # fastmode is on
                hasattr(obj, 'identifier') and
                hasattr(obj, 'legislative_session')
            ):
                identifier = obj.identifier
                session = obj.legislative_session
                jurisdiction = upload_file_path[:2].upper()

                s3 = boto3.client("s3")
                bucket = settings.S3_BILLS_BUCKET

                try:
                    self.info(f"Checking for existing {identifier} in bill cache")
                    new_json = obj.as_dict()

                    if self.existing_session_bills is None:
                        self.existing_session_bills = self.get_elastic_entries(
                            new_json, jurisdiction
                        )

                    if existing_json := self.existing_session_bills.get(identifier):
                        new_json = replace_none_in_dict(new_json)
                        existing_json = replace_none_in_dict(existing_json)

                        mismatched_fields = {
                            key
                            for key in new_json.keys()
                            if new_json[key] != existing_json.get(key)
                        }

                        if mismatched_fields - {"_id", "jurisdiction", "scraped_at"}:
                            self.info(
                                f"Bill changed, saving: {jurisdiction}/{session}/{identifier}"
                            )
                        else:
                            self.info(
                                f"Bill unchanged — skipping save: {jurisdiction}/{session}/{identifier}"
                            )
                            return
                    else:
                        self.info(
                            f"Bill not found in elastic, saving: {jurisdiction}/{session}/{identifier}"
                        )
                except Exception as e:
                    self.warning(f"S3 comparison failed for {identifier}: {e}")


            if self.kafka:  # Send to Kafka only if producer is initialized
                bill_data = obj.as_dict()
                bill_data.pop("jurisdiction", None)
                bill_data.pop("scraped_at", None)
                self.kafka_producer.send(jurisdiction.upper(), bill_data)
                # Kafka producers use batching to optimize throughput and reduce the load on brokers
                # The delay below ensures messages are sent before the script continues
                # Documentation: https://kafka.apache.org/documentation/#producerconfigs_linger.ms
                time.sleep(0.1)
                logging.info(f'{obj._type} {obj} sent to Kafka.')
                self.kafka_producer.flush()
            elif self.realtime:
                self.output_file_path = str(upload_file_path)

                s3 = boto3.client('s3')
                bucket = settings.S3_REALTIME_BASE.removeprefix('s3://')

                s3.put_object(
                    Body=json.dumps(
                        OrderedDict(sorted(obj.as_dict().items())),
                        cls=utils.JSONEncoderPlus,
                        separators=(',', ': '),
                    ),
                    Bucket=bucket,
                    Key=self.output_file_path,
                )

                self.push_to_queue()
            else:
                with open(file_path, 'w') as f:
                    json.dump(obj.as_dict(), f, cls=utils.JSONEncoderPlus)

        else:
            self.scrape_output_handler.handle(obj)

        # validate after writing, allows for inspection on failure
        try:
            obj.validate()
        except ValueError as ve:
            if self.strict_validation:
                raise ve
            else:
                self.warning(ve)

        # after saving and validating, save subordinate objects
        for obj in obj._related:
            self.save_object(obj)

    def do_scrape(self, **kwargs):
        record = {'objects': defaultdict(int)}
        self.output_names = defaultdict(set)
        record['start'] = utils.utcnow()
        try:
            for obj in self.scrape(**kwargs) or []:
                # allow for returning empty objects in a list
                if not obj:
                    continue
                if hasattr(obj, '__iter__'):
                    for iterobj in obj:
                        self.save_object(iterobj)
                else:
                    self.save_object(obj)
        except EmptyScrape:
            if self.output_names:
                raise ScrapeError(
                    f'objects returned from {self.__class__.__name__} scrape, expected none'
                )
            self.warning(
                f'{self.__class__.__name__} raised EmptyScrape, continuing without any results'
            )
        else:
            if not self.output_names:
                raise ScrapeError(
                    'no objects returned from {} scrape'.format(self.__class__.__name__)
                )

        record['end'] = utils.utcnow()
        record['skipped'] = getattr(self, 'skipped', 0)
        for _type, nameset in self.output_names.items():
            record['objects'][_type] += len(nameset)

        return record

    def scrape(self, **kwargs):
        raise NotImplementedError(
            self.__class__.__name__ + ' must provide a scrape() method'
        )


class BaseBillScraper(Scraper):
    skipped = 0

    class ContinueScraping(Exception):
        '''indicate that scraping should continue without saving an object'''

        pass

    def scrape(self, legislative_session, **kwargs):
        self.legislative_session = legislative_session
        for bill_id, extras in self.get_bill_ids(**kwargs):
            try:
                yield self.get_bill(bill_id, **extras)
            except self.ContinueScraping as exc:
                self.warning('skipping %s: %r', bill_id, exc)
                self.skipped += 1
                continue


class BaseModel(object):
    '''
    This is the base class for all the Open Civic objects. This contains
    common methods and abstractions for OCD objects.
    '''

    # to be overridden by children. Something like 'person' or 'organization'.
    # Used in :func:`validate`.
    _type = None
    _schema = None

    def __init__(self):
        super(BaseModel, self).__init__()
        self._id = str(uuid.uuid1())
        self._related = []
        self.extras = {}

    # validation

    def validate(self, schema=None):
        '''
        Validate that we have a valid object.

        On error, this will raise a `ScrapeValueError`

        This also expects that the schemas assume that omitting required
        in the schema asserts the field is optional, not required. This is
        due to upstream schemas being in JSON Schema v3, and not validictory's
        modified syntax.
        ^ TODO: FIXME
        '''
        if schema is None:
            schema = self._schema

        # this code copied to openstates/cli/validate - maybe update it if changes here :)
        type_checker = Draft3Validator.TYPE_CHECKER.redefine(
            'datetime', lambda c, d: isinstance(d, (datetime.date, datetime.datetime))
        )
        type_checker = type_checker.redefine(
            'date',
            lambda c, d: (
                isinstance(d, datetime.date) and not isinstance(d, datetime.datetime)
            ),
        )

        ValidatorCls = jsonschema.validators.extend(
            Draft3Validator, type_checker=type_checker
        )
        validator = ValidatorCls(schema, format_checker=FormatChecker())

        errors = [str(error) for error in validator.iter_errors(self.as_dict())]
        if errors:
            raise ScrapeValueError(
                'validation of {} {} failed: {}'.format(
                    self.__class__.__name__, self._id, '\n\t' + '\n\t'.join(errors)
                )
            )

    def pre_save(self, jurisdiction):
        pass

    def as_dict(self):
        d = {}
        for attr in self._schema['properties'].keys():
            if hasattr(self, attr):
                d[attr] = getattr(self, attr)
        d['_id'] = self._id
        return d

    # operators

    def __setattr__(self, key, val):
        if key[0] != '_' and key not in self._schema['properties'].keys():
            raise ScrapeValueError(
                'property "{}" not in {} schema'.format(key, self._type)
            )
        super(BaseModel, self).__setattr__(key, val)

    def add_scrape_metadata(self, jurisdiction):
        """Add scrape metadata"""
        self.jurisdiction = {
            "id": jurisdiction.jurisdiction_id,
            "name": jurisdiction.name,
            "classification": jurisdiction.classification,
            "division_id": jurisdiction.division_id,
        }

        self.scraped_at = utils.utcnow()


class SourceMixin(object):
    def __init__(self):
        super(SourceMixin, self).__init__()
        self.sources = []

    def add_source(self, url, *, note=''):
        '''Add a source URL from which data was collected'''
        new = {'url': url, 'note': note}
        self.sources.append(new)


class LinkMixin(object):
    def __init__(self):
        super(LinkMixin, self).__init__()
        self.links = []

    def add_link(self, url, *, note=''):
        self.links.append({'note': note, 'url': url})


class AssociatedLinkMixin(object):
    def _add_associated_link(
        self,
        collection,
        note,
        url,
        *,
        media_type,
        on_duplicate='warn',
        date='',
        classification='',
    ):
        if on_duplicate not in ['error', 'ignore', 'warn']:
            raise ScrapeValueError('on_duplicate must be "warn", "error" or "ignore"')

        try:
            associated = getattr(self, collection)
        except AttributeError:
            associated = self[collection]

        ver = {
            'note': note,
            'links': [],
            'date': date,
            'classification': classification,
        }

        # keep a list of the links we've seen, we need to iterate over whole list on each add
        # unfortunately this means adds are O(n)
        seen_links = set()

        matches = 0
        for item in associated:
            for link in item['links']:
                seen_links.add(link['url'])

            if all(
                ver.get(x) == item.get(x) for x in ['note', 'date', 'classification']
            ):
                matches = matches + 1
                ver = item

        # it should be impossible to have multiple matches found unless someone is bypassing
        # _add_associated_link
        assert matches <= 1, 'multiple matches found in _add_associated_link'

        if url in seen_links:
            if on_duplicate == 'error':
                raise ScrapeValueError(
                    'Duplicate entry in "%s" - URL: "%s"' % (collection, url)
                )
            elif on_duplicate == 'warn':
                # default behavior: same as ignore but logs an warning so people can fix
                logging.getLogger('openstates').warning(
                    f'Duplicate entry in "{collection}" - URL: {url}'
                )
                return None
            else:
                # This means we're in ignore mode. This situation right here
                # means we should *skip* adding this link silently and continue
                # on with our scrape. This should *ONLY* be used when there's
                # a site issue (Version 1 == Version 2 because of a bug) and
                # *NEVER* because 'Current' happens to match 'Version 3'. Fix
                # that in the scraper, please.
                #  - PRT
                return None

        # OK. This is either new or old. Let's just go for it.
        ret = {'url': url, 'media_type': media_type}

        ver['links'].append(ret)

        if matches == 0:
            # in the event we've got a new entry; let's just insert it into
            # the versions on this object. Otherwise it'll get thrown in
            # automagically.
            associated.append(ver)

        return ver
