import boto3  # noqa
import datetime
from http.client import RemoteDisconnected
from google.cloud import storage  # type: ignore
import importlib
import json
import jsonschema
import logging
import os
import random
import requests
import scrapelib
import subprocess
import time
from urllib.error import URLError
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
GCP_PROJECT = os.environ.get("GCP_PROJECT", None)
BUCKET_NAME = os.environ.get("BUCKET_NAME", None)
SCRAPE_REALTIME_LAKE_PREFIX = os.environ.get(
    "SCRAPE_REALTIME_LAKE_PREFIX", "legislation/realtime"
)


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

def get_random_user_agent():
    """
    Return a random user agent to help avoid detection.
    """
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPad; CPU OS 14_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36 Edg/91.0.864.59",
    ]
    return random.choice(user_agents)


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
        http_resilience_mode=False,
    ):
        super(Scraper, self).__init__()

        # set options
        self.jurisdiction = jurisdiction
        self.datadir = datadir
        self.realtime = realtime
        self.kafka = kafka
        self.kafka_producer = kafka_producer
        self.file_archiving_enabled = file_archiving_enabled


        # HTTP connection resilience settings
        self.http_resilience_mode = http_resilience_mode
        self.http_resilience_headers = {}
        # http resilience: Set up a circuit breaker to track consecutive failures
        self._consecutive_failures = 0
        self._max_consecutive_failures = 3
        self._circuit_breaker_timeout = 120  # 2 minutes
        # http resilience: Set up connection pool reset
        self._last_reset_time = time.time()
        self._reset_interval = 600  # Reset connection pool every 10 minutes
        self._random_delay_on_failure_min = 5
        self._random_delay_on_failure_max = 15

        # scrapelib setup
        self.timeout = settings.SCRAPELIB_TIMEOUT
        self.requests_per_minute = settings.SCRAPELIB_RPM
        self.retry_attempts = settings.SCRAPELIB_RETRY_ATTEMPTS
        self.retry_wait_seconds = settings.SCRAPELIB_RETRY_WAIT_SECONDS
        self.verify = settings.SCRAPELIB_VERIFY

        # output
        self.output_file_path = None
        self._realtime_upload_data_classes = settings.REALTIME_UPLOAD_DATA_CLASSES
        self._upload_interval = 60 * 15  # 15 minutes
        self._last_upload_time = time.time()

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

    def _upload_jsonl_to_gcs(self):
        cloud_storage_client = storage.Client(project=GCP_PROJECT)
        bucket = cloud_storage_client.bucket(BUCKET_NAME)

        for upload_data_class in self._realtime_upload_data_classes:
            jsonl_path = os.path.join(self.datadir, f"{upload_data_class}.jsonl")
            if os.path.exists(jsonl_path) and os.path.getsize(jsonl_path) > 0:
                now = datetime.datetime.now(datetime.timezone.utc)
                scrape_year_month = now.strftime("%Y-%m")
                timestamp = now.isoformat()
                jurisdiction_id = self.jurisdiction.jurisdiction_id.replace(
                    "ocd-jurisdiction/", ""
                )
                dest_file_path = f"{SCRAPE_REALTIME_LAKE_PREFIX}/{upload_data_class}/{jurisdiction_id}/{scrape_year_month}/{upload_data_class}_{timestamp}.jsonl"

                blob = bucket.blob(dest_file_path)
                blob.upload_from_filename(jsonl_path)
                self.logger.info(
                    f"Uploaded {upload_data_class} to GCS: {dest_file_path}"
                )
                # Delete the local file after upload
                os.remove(jsonl_path)

    def upload_to_gcs_real_time(self, obj=None, force_upload=False):
        """
        Save scrape output to object bucket every interval
        """
        if GCP_PROJECT is None or BUCKET_NAME is None:
            self.logger.warning(
                "Real-time Upload missing necessary settings are missing. No upload was done."
            )
            return

        # Attempt to save only when there is an object.
        if obj:
            obj_dict = obj.as_dict()
            upload_data_class = obj._type

            if upload_data_class not in self._realtime_upload_data_classes:
                raise ScrapeError(
                    f"Unsupported data class for gcs_real_time_upload {upload_data_class}"
                )
                return

            jsonl_path = os.path.join(self.datadir, f"{upload_data_class}.jsonl")
            with open(jsonl_path, "a") as f:
                json.dump(obj_dict, f, cls=utils.JSONEncoderPlus)
                f.write("\n")

        now = time.time()
        if force_upload or now - self._last_upload_time >= self._upload_interval:
            self._upload_jsonl_to_gcs()
            self._last_upload_time = now

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
            with open(file_path, "w") as f:
                json.dump(obj.as_dict(), f, cls=utils.JSONEncoderPlus)

            # Periodically push data to GCS by data class
            if self.realtime:
                self.upload_to_gcs_real_time(obj)

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

    def request_resiliently(self, request_func):
        try:
            # Reset connection pool if needed
            self._reset_connection_pool_if_needed()

            # Add a random delay between processing items
            self.add_random_delay(1, 3)

            # If we've had too many consecutive failures, pause for a while
            if self._consecutive_failures >= self._max_consecutive_failures:
                self.logger.warning(
                    f"Circuit breaker triggered after {self._consecutive_failures} consecutive failures. "
                    f"Pausing for {self._circuit_breaker_timeout} seconds."
                )
                time.sleep(self._circuit_breaker_timeout)
                self._consecutive_failures = 0

                # Rotate user agent after circuit breaker timeout
                self.headers["User-Agent"] = get_random_user_agent()

            response = self.retry_on_connection_error(
                request_func,
                max_retries=3,
                initial_backoff=10,
                max_backoff=120,
            )

            # Reset consecutive failures counter on success
            self._consecutive_failures = 0

            return response
        except Exception as e:
            self._consecutive_failures += 1
            self.logger.error(f"Error processing item: {e}")

            # If it's a connection error, add a longer delay
            if isinstance(e, (ConnectionError, RemoteDisconnected)):
                self.logger.warning("Connection error. Adding longer delay.")
                self.add_random_delay(
                    self._random_delay_on_failure_min, self._random_delay_on_failure_max
                )

                # Rotate user agent after connection error
                self.headers["User-Agent"] = get_random_user_agent()

    def get(self, url, **kwargs):
        request_func = lambda: super(Scraper, self).get(url, **kwargs)  # noqa: E731
        if self.http_resilience_mode:
            return self.request_resiliently(request_func)
        else:
            return super().get(url, **kwargs)

    def post(self, url, data=None, json=None, **kwargs):
        request_func = lambda: super(Scraper, self).post(url, data=data, json=json**kwargs)  # noqa: E731
        if self.http_resilience_mode:
            return self.request_resiliently(request_func)
        else:
            return super().post(url, data=data, json=json, **kwargs)

    def retry_on_connection_error(
        self, func, max_retries=5, initial_backoff=2, max_backoff=60
    ):
        """
        Retry a function call on connection errors with exponential backoff.

        Args:
            func: Function to call
            max_retries: Maximum number of retries
            initial_backoff: Initial backoff time in seconds
            max_backoff: Maximum backoff time in seconds

        Returns:
            The result of the function call
        """
        retries = 0
        backoff = initial_backoff

        while True:
            try:
                return func()
            except (
                ConnectionError,
                RemoteDisconnected,
                URLError,
                requests.exceptions.Timeout,
                requests.exceptions.RequestException,
            ) as e:
                retries += 1
                if retries > max_retries:
                    self.logger.error(
                        f"Max retries ({max_retries}) exceeded. Last error: {e}"
                    )
                    raise

                # Calculate backoff with jitter
                jitter = random.uniform(0.8, 1.2)
                current_backoff = min(backoff * jitter, max_backoff)

                self.logger.warning(
                    f"Connection error: {e}. Retrying in {current_backoff:.2f} seconds (attempt {retries}/{max_retries})"
                )
                time.sleep(current_backoff)

                # Increase backoff for next retry
                backoff = min(backoff * 2, max_backoff)

    def _create_fresh_session(self):
        """
        Create a fresh session with appropriate settings.
        """
        if hasattr(self, "session"):
            self.session.close()

        # Create a new session
        self.session = requests.Session()

        # Set any custom headers
        self.session.headers.update(self.http_resilience_headers)

        # Set up retry mechanism
        adapter = requests.adapters.HTTPAdapter(
            max_retries=self.retry_attempts,
            pool_connections=10,
            pool_maxsize=10,
            pool_block=False,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.headers["User-Agent"] = get_random_user_agent()

        self.logger.info(
            f"Created fresh session with user agent: {self.headers['User-Agent']}"
        )

        return self.session

    def _reset_connection_pool_if_needed(self):
        """
        Reset the connection pool if it's been too long since the last reset.
        This helps prevent "Remote end closed connection without response" errors.
        """
        current_time = time.time()
        if current_time - self._last_reset_time > self._reset_interval:
            self.logger.info(
                f"Resetting connection pool after {self._reset_interval} seconds"
            )

            # Create a fresh session
            self._create_fresh_session()

            self._last_reset_time = current_time

    def add_random_delay(self, min_seconds=1, max_seconds=3):
        """
        Add a random delay to simulate human behavior.

        Args:
            min_seconds: Minimum delay in seconds
            max_seconds: Maximum delay in seconds
        """
        delay = random.uniform(min_seconds, max_seconds)
        self.logger.debug(f"Adding random delay of {delay:.2f} seconds")
        time.sleep(delay)


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
        on_duplicate="warn",
        date="",
        classification="",
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
                ver.get(x) == item.get(x) for x in ["note", "date", "classification"]
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
