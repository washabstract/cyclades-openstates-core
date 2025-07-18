import argparse
import boto3
import contextlib
import datetime
import glob
from google.cloud import storage  # type: ignore
import importlib
import inspect
import json
import logging
import logging.config
import os
import subprocess
import sys
import time
import traceback
import typing
from collections import defaultdict
from kafka import KafkaProducer
from types import ModuleType

from django.db import transaction  # type: ignore

from .. import settings, utils
from ..exceptions import CommandError
from ..scrape import JurisdictionScraper, State
from ..utils.django import init_django
from ..utils.instrument import Instrumentation
from .reports import generate_session_report, print_report, save_report

logger = logging.getLogger('openstates')
stats = Instrumentation()

ALL_ACTIONS = ('scrape', 'import')

# Settings to archive scraped out put to GCP Cloud Storage
GCP_PROJECT = os.environ.get('GCP_PROJECT', None)
BUCKET_NAME = os.environ.get('BUCKET_NAME', None)
SCRAPE_LAKE_PREFIX = os.environ.get('BUCKET_PREFIX', 'legislation')
DAG_RUN_START = os.environ.get("DAG_RUN_START", None)

class _Unset:
    pass


UNSET = _Unset()


@contextlib.contextmanager
def override_settings(settings, overrides):  # type: ignore
    original = {}
    for key, value in overrides.items():
        original[key] = getattr(settings, key, UNSET)
        setattr(settings, key, value)
    yield
    for key, value in original.items():
        if value is UNSET:
            delattr(settings, key)
        else:
            setattr(settings, key, value)


def get_jurisdiction(module_name: str) -> tuple[State, ModuleType]:
    # get the state object
    module = importlib.import_module(module_name)
    for obj in module.__dict__.values():
        # ensure we're dealing with a subclass of State
        if isinstance(obj, type) and issubclass(obj, State) and obj != State:
            return obj(), module
    raise CommandError(f'Unable to import State subclass from {module_name}')


def init_kafka_producer(kafka_cluster_name: str) -> KafkaProducer:
    client = boto3.client('kafka', region_name='us-west-2')

    # Grab Cluster Arn
    clusters = client.list_clusters()['ClusterInfoList']
    cluster_arn = None
    for cluster in clusters:
        if cluster['ClusterName'] == kafka_cluster_name:
            cluster_arn = cluster['ClusterArn']
            break

    if cluster_arn is None:
        raise ValueError(f'No Kafka cluster found with name: {kafka_cluster_name}')

    # Grab Brokers
    response = client.get_bootstrap_brokers(ClusterArn=cluster_arn)
    kafka_brokers = response['BootstrapBrokerStringTls']

    producer = KafkaProducer(
        security_protocol='SSL',
        bootstrap_servers=kafka_brokers,
        value_serializer=lambda v: json.dumps(v, cls=utils.JSONEncoderPlus).encode(
            'utf-8'
        ),
    )

    return producer

def do_scrape(
    juris: State,
    args: argparse.Namespace,
    scrapers: dict[str, dict[str, str]],
    active_sessions: set[str],
) -> dict[str, typing.Any]:
    # make output and cache dirs
    utils.makedirs(settings.CACHE_DIR)
    datadir = os.path.join(settings.SCRAPED_DATA_DIR, args.module)
    utils.makedirs(datadir)
    # clear json from data dir
    for f in glob.glob(datadir + '/*.json'):
        os.remove(f)

    kafka_producer = init_kafka_producer(args.kafka) if args.kafka else None

    report = {}

    # do jurisdiction
    jscraper = JurisdictionScraper(
        juris,
        datadir,
        strict_validation=args.strict,
        fastmode=args.fastmode,
        realtime=args.realtime,
        kafka=args.kafka,
        kafka_producer=kafka_producer,
        file_archiving_enabled=args.archive,
    )
    report['jurisdiction'] = jscraper.do_scrape()
    stats.write_stats(
        [
            {
                'metric': 'jurisdiction_scrapes',
                'fields': {'total': 1},
                'tags': {'jurisdiction': juris.name},
            }
        ]
    )

    if args.fastmode:
        logger.info("Fastmode is enabled: Bill cache will be used with elasticsearch")

    last_scrape_datetime = DAG_RUN_START or datetime.datetime.utcnow().isoformat()
    for scraper_name, scrape_args in scrapers.items():
        ScraperCls = juris.scrapers[scraper_name]
        if (
            'session' in inspect.getfullargspec(ScraperCls.scrape).args
            and 'session' not in scrape_args
        ):
            logger.warning(
                f'no session provided, using active sessions: {active_sessions}'
            )
            # handle automatically setting session if required by the scraper
            # the report logic was originally meant for one run, so we combine the start & end times
            # and counts here
            report[scraper_name] = {
                'start': None,
                'end': None,
                'objects': defaultdict(int),
            }
            for session in active_sessions:
                # new scraper each time
                scraper = ScraperCls(
                    juris,
                    datadir,
                    strict_validation=args.strict,
                    fastmode=args.fastmode,
                    realtime=args.realtime,
                    kafka=args.kafka,
                    kafka_producer=kafka_producer,
                    file_archiving_enabled=args.archive,
                )
                partial_report = scraper.do_scrape(**scrape_args, session=session)
                stats.write_stats(
                    [
                        {
                            'metric': 'session_scrapes',
                            'fields': {'total': 1},
                            'tags': {'jurisdiction': juris.name, 'session': session},
                        }
                    ]
                )
                if not report[scraper_name]['start']:
                    report[scraper_name]['start'] = partial_report['start']
                report[scraper_name]['end'] = partial_report['end']
                for obj, val in partial_report['objects'].items():
                    report[scraper_name]['objects'][obj] += val
                stats.write_stats(
                    [
                        {
                            'metric': 'last_session_scrape',
                            'fields': {'time': int(time.time())},
                            'tags': {'jurisdiction': juris.name, 'session': session},
                        }
                    ]
                )
                if args.realtime:
                    scraper.upload_to_gcs_real_time(force_upload=True)
        else:
            scraper = ScraperCls(
                juris,
                datadir,
                strict_validation=args.strict,
                fastmode=args.fastmode,
                realtime=args.realtime,
                kafka=args.kafka,
                kafka_producer=kafka_producer,
                file_archiving_enabled=args.archive,
            )
            report[scraper_name] = scraper.do_scrape(**scrape_args)
            session = scrape_args.get("session", "")
            if args.realtime:
                scraper.upload_to_gcs_real_time(force_upload=True)
            if session:
                stats.write_stats(
                    [
                        {
                            'metric': 'session_scrapes',
                            'fields': {'total': 1},
                            'tags': {'jurisdiction': juris.name, 'session': session},
                        },
                        {
                            'metric': 'last_session_scrape',
                            'fields': {'time': int(time.time())},
                            'tags': {'jurisdiction': juris.name, 'session': session},
                        },
                    ]
                )
            else:
                stats.write_stats(
                    [
                        {
                            'metric': 'non_session_scrapes',
                            'fields': {'total': 1},
                            'tags': {'jurisdiction': juris.name},
                        },
                        {
                            'metric': 'last_non_session_scrape',
                            'fields': {'time': int(time.time())},
                            'tags': {'jurisdiction': juris.name},
                        },
                    ]
                )

    # optionally upload scrape output to cloud storage
    # archive and realtime BOTH coexist for now, as we refactor realtime
    if args.archive:  # and not args.realtime:
        archive_to_cloud_storage(datadir, juris, last_scrape_datetime)

    return report


def archive_to_cloud_storage(
    datadir: str, juris: State, last_scrape_datetime: str
) -> None:
    # check if we have necessary settings
    if GCP_PROJECT is None or BUCKET_NAME is None:
        logger.error(
            "Scrape archiving is turned on, but necessary settings are missing. No archive was done."
        )
        return
    logger.info("Beginning archive of scraped files to google cloud storage.")
    logger.info(f"GCP Project is {GCP_PROJECT} and bucket is {BUCKET_NAME}")

    # Catch exceptions so that we do not fail the scrape if transient GCS error occurs
    try:
        cloud_storage_client = storage.Client(project=GCP_PROJECT)
        bucket = cloud_storage_client.bucket(BUCKET_NAME)
        jurisdiction_id = juris.jurisdiction_id.replace("ocd-jurisdiction/", "")
        destination_prefix = (
            f"{SCRAPE_LAKE_PREFIX}/{jurisdiction_id}/{last_scrape_datetime}"
        )

        # read files in directory and upload
        files_count = 0
        for file_path in glob.glob(datadir + "/*.json"):
            files_count += 1
            blob_name = os.path.join(destination_prefix, os.path.basename(file_path))
            blob = bucket.blob(blob_name)
            blob.upload_from_filename(file_path)

        logger.info(
            f"Completed archive to Google Cloud Storage, {files_count} files "
            f"were uploaded to {destination_prefix}."
        )

    except Exception as e:
        logger.warning(
            f"An error occurred during the attempt to archive files to Google Cloud Storage: {e}"
        )



def do_import(juris: State, args: argparse.Namespace) -> dict[str, typing.Any]:
    # import inside here because to avoid loading Django code unnecessarily
    from openstates.data.models import Jurisdiction as DatabaseJurisdiction
    from openstates.importers import (
        BillImporter,
        EventImporter,
        JurisdictionImporter,
        VoteEventImporter,
    )

    datadir = os.path.join(settings.SCRAPED_DATA_DIR, args.module)

    juris_importer = JurisdictionImporter(juris.jurisdiction_id)
    bill_importer = BillImporter(juris.jurisdiction_id)
    vote_event_importer = VoteEventImporter(juris.jurisdiction_id, bill_importer)
    event_importer = EventImporter(juris.jurisdiction_id, vote_event_importer)
    report = {}

    with transaction.atomic():
        logger.info('import jurisdictions...')
        report.update(juris_importer.import_directory(datadir))
        logger.info("import bills...")
        report.update(
            bill_importer.import_directory(
                datadir, allow_duplicates=args.allow_duplicates
            )
        )
        logger.info("import vote events...")
        report.update(
            vote_event_importer.import_directory(
                datadir, allow_duplicates=args.allow_duplicates
            )
        )
        logger.info("import events...")
        report.update(
            event_importer.import_directory(
                datadir, allow_duplicates=args.allow_duplicates
            )
        )
        DatabaseJurisdiction.objects.filter(id=juris.jurisdiction_id).update(
            latest_bill_update=datetime.datetime.utcnow()
        )

    # compile info on all sessions that were updated in this run
    seen_sessions = set()
    seen_sessions.update(bill_importer.get_seen_sessions())
    seen_sessions.update(vote_event_importer.get_seen_sessions())
    for session in seen_sessions:
        generate_session_report(session)

    return report


def check_session_list(juris: State) -> set[str]:
    scraper = type(juris).__name__

    # if get_session_list is not defined
    if not hasattr(juris, 'get_session_list'):
        raise CommandError(f'{scraper}.get_session_list() is not provided')

    scraped_sessions = juris.get_session_list()

    if not scraped_sessions:
        raise CommandError('no sessions from {}.get_session_list()'.format(scraper))

    active_sessions = set()
    # copy the list to avoid modifying it
    sessions = set(juris.ignored_scraped_sessions)
    session_identifiers = set(juris.ignored_scraped_sessions)
    for session in juris.legislative_sessions:
        sessions.add(session.get("_scraped_name", session["identifier"]))
        session_identifiers.add(session.get("identifier"))
        if session.get("active") or ("all" in getattr(juris, "backfill", [])):
            active_sessions.add(session.get("identifier"))
    active_sessions.update(juris.backfill)
    if not active_sessions:
        raise CommandError(f"No active sessions on {scraper}")

    unaccounted_sessions = list(set(scraped_sessions) - sessions)
    if unaccounted_sessions:
        unaccounted_sessions_identifiers = list(set(scraped_sessions) - session_identifiers)
        if unaccounted_sessions_identifiers:
            raise CommandError(
                (
                    "Session(s) {sessions} were reported by {scraper}.get_session_list() "
                    "but were not found in {scraper}.legislative_sessions or "
                    "{scraper}.ignored_scraped_sessions."
                ).format(sessions=", ".join(unaccounted_sessions), scraper=scraper)
            )
    stats.write_stats(
        [
            {
                'metric': 'sessions',
                'fields': {'count': len(active_sessions)},
                'tags': {'jurisdiction': scraper, 'session_type': 'active'},
            },
            {
                'metric': 'sessions',
                'fields': {'count': len(unaccounted_sessions)},
                'tags': {'jurisdiction': scraper, 'session_type': 'unaccounted'},
            },
            {
                'metric': 'sessions',
                'fields': {'count': len(juris.ignored_scraped_sessions)},
                'tags': {'jurisdiction': scraper, 'session_type': 'ignored'},
            },
        ]
    )
    return active_sessions


def do_update(
    args: argparse.Namespace, other: list[str], juris: State
) -> dict[str, typing.Any]:
    available_scrapers = getattr(juris, 'scrapers', {})
    default_scrapers = getattr(juris, 'default_scrapers', None)
    scrapers: dict[str, dict[str, str]] = {}

    if not available_scrapers:
        raise CommandError('no scrapers defined on jurisdiction')

    if other:
        # parse arg list in format: (scraper (k:v)+)+
        cur_scraper = None
        for arg in other:
            if '=' in arg:
                if not cur_scraper:
                    raise CommandError('argument {} before scraper name'.format(arg))
                k, v = arg.split('=', 1)
                scrapers[cur_scraper][k] = v
            elif arg in juris.scrapers:
                cur_scraper = arg
                scrapers[cur_scraper] = {}
            else:
                raise CommandError(
                    'no such scraper: module={} scraper={}'.format(args.module, arg)
                )
    elif default_scrapers is not None:
        scrapers = {s: {} for s in default_scrapers}
    else:
        scrapers = {key: {} for key in available_scrapers.keys()}

    # modify args in-place so we can pass them around
    if not args.actions:
        args.actions = ALL_ACTIONS
    
    if args.backfill:
        juris.backfill = args.backfill

    if 'import' in args.actions:
        init_django()

    # print the plan
    report = {
        'plan': {'module': args.module, 'actions': args.actions, 'scrapers': scrapers},
        'start': utils.utcnow(),
    }
    print_report(report)

    if 'scrape' in args.actions:
        active_sessions = check_session_list(juris)

    try:
        if 'scrape' in args.actions:
            report['scrape'] = do_scrape(juris, args, scrapers, active_sessions)
            stats.write_stats(
                [
                    {
                        'metric': 'last_collection_run',
                        'fields': {'time': int(time.time())},
                        'tags': {
                            'jurisdiction': juris.name,
                            'scrape_type': 'scrape',
                        },
                    }
                ]
            )
            # Update remote S3 cache if ARCHIVE_CACHE_TO_S3 is set
            if os.environ.get('ARCHIVE_CACHE_TO_S3', 'false').lower() == 'true':
                try:
                    logger.info(f'Syncing cache directory {settings.CACHE_DIR} to S3 bucket {settings.CACHE_BUCKET}')
                    subprocess.run(['aws', 's3', 'sync', settings.CACHE_DIR, settings.CACHE_BUCKET+'/'+juris.name], check=True)
                    logger.info('Cache directory successfully synced to S3.')
                except subprocess.CalledProcessError as e:
                    logger.error(f'Failed to sync cache directory to S3: {e}')
        # we skip import in realtime mode since this happens via the lambda function
        # realtime and normal import coexist for now as we refactor realtime
        if "import" in args.actions:  # and not args.realtime:
            report["import"] = do_import(juris, args)
            stats.write_stats(
                [
                    {
                        'metric': 'last_collection_run',
                        'fields': {'time': int(time.time())},
                        'tags': {
                            'jurisdiction': juris.name,
                            'scrape_type': 'import',
                        },
                    }
                ]
            )
        report['success'] = True
    except Exception as exc:
        stats.write_stats(
            [
                {
                    'metric': 'scraper_failures',
                    'fields': {'total': 1},
                    'tags': {
                        'jurisdiction': juris.name,
                        'scrapers': ','.join(sorted(args.actions)),
                    },
                }
            ]
        )
        stats.close()
        report['success'] = False
        report['exception'] = exc
        report['traceback'] = traceback.format_exc()
        if 'import' in args.actions:
            save_report(report, juris.jurisdiction_id)
        raise
    else:
        finish = utils.utcnow()

        for scrape_type, details in report.get('scrape', {}).items():  # type: ignore
            # datetime - datetime = timedelta object, which has a 'seconds' attribute
            stats.write_stats(
                [
                    {
                        'metric': 'scrape_runtime',
                        'fields': {'secs': (finish - details['start']).seconds},
                        'tags': {
                            'jurisdiction': juris.name,
                            'scrape_type': scrape_type,
                        },
                    }
                ]
            )
            for objtype, num in details['objects'].items():
                stats.write_stats(
                    [
                        {
                            'metric': 'objects',
                            'fields': {'collected': num},
                            'tags': {
                                'jurisdiction': juris.name,
                                'scrape_type': scrape_type,
                                'object_type': objtype,
                            },
                        }
                    ]
                )
        for scrape_type, details in report.get('import', {}).items():  # type: ignore
            for import_type in ['insert', 'update', 'noop']:
                stats.write_stats(
                    [
                        {
                            'metric': 'objects',
                            'fields': {'imported': details[import_type]},
                            'tags': {
                                'jurisdiction': juris.name,
                                'scrape_type': scrape_type,
                                'import_type': import_type,
                            },
                        }
                    ]
                )

        if 'import' in args.actions:
            save_report(report, juris.jurisdiction_id)

        print_report(report)
        return report


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser('openstates', description='openstates CLI')
    parser.add_argument('--debug', action='store_true', help='open debugger on error')
    parser.add_argument(
        '--loglevel',
        default='INFO',
        help=(
            'set log level. options are: '
            'DEBUG|INFO|WARNING|ERROR|CRITICAL '
            '(default is INFO)'
        ),
    )
    # what to scrape
    parser.add_argument('module', type=str, help='path to scraper module')
    for arg in ALL_ACTIONS:
        parser.add_argument(
            '--' + arg,
            dest='actions',
            action='append_const',
            const=arg,
            help='only run {} post-scrape step'.format(arg),
        )

    # scraper arguments
    parser.add_argument(
        '--nonstrict',
        action='store_false',
        dest='strict',
        help='skip validation on save',
    )
    parser.add_argument(
        "--allow_duplicates",
        action="store_true",
        dest="allow_duplicates",
        help="Skip throwing a DuplicateItemError, instead all import of duplicate items",
    )
    parser.add_argument(
        "--fastmode", action="store_true", help="use cache and turn off throttling"
    )

    # settings overrides
    parser.add_argument('--datadir', help='data directory', dest='SCRAPED_DATA_DIR')
    parser.add_argument('--cachedir', help='cache directory', dest='CACHE_DIR')
    parser.add_argument(
        '-r', '--rpm', help='scraper rpm', type=int, dest='SCRAPELIB_RPM'
    )
    parser.add_argument(
        '--timeout', help='scraper timeout', type=int, dest='SCRAPELIB_TIMEOUT'
    )
    parser.add_argument(
        '--no-verify',
        help='skip tls verification',
        action='store_false',
        dest='SCRAPELIB_VERIFY',
    )
    parser.add_argument(
        '--retries', help='scraper retries', type=int, dest='SCRAPELIB_RETRIES'
    )
    parser.add_argument(
        '--retry_wait',
        help='scraper retry wait',
        type=int,
        dest='SCRAPELIB_RETRY_WAIT_SECONDS',
    )

    # realtime mode
    parser.add_argument('--realtime', action='store_true', help='enable realtime mode')

    # kafka mode
    parser.add_argument('--kafka', type=str, help='Enable writes to Kafka (MSK)')

    # backfill mode
    parser.add_argument(
        '--backfill',
        nargs='+',
        help='backfill mode for specific sessions, e.g. --backfill 2025-2026 2024-2025',
    )

    # Archiving realtime processing JSON files
    parser.add_argument(
        '--archive',
        action='store_true',
        help='enable archiving of realtime processing JSON files, defaults to false',
    )

    # process args
    return parser.parse_known_args()


def main() -> int:
    args, other = parse_args()

    # set log level from command line
    handler_level = getattr(logging, args.loglevel.upper(), 'INFO')
    settings.LOGGING['handlers']['default']['level'] = handler_level  # type: ignore
    logging.config.dictConfig(settings.LOGGING)
    stats.logger.setLevel(handler_level)

    # turn debug on
    if args.debug:
        try:
            debug_module = importlib.import_module('ipdb')
        except ImportError:
            debug_module = importlib.import_module('pdb')

        # turn on PDB-on-error mode
        # stolen from http://stackoverflow.com/questions/1237379/
        # if this causes problems in interactive mode check that page
        def _tb_info(type, value, tb):  # type: ignore
            traceback.print_exception(type, value, tb)
            debug_module.pm()

        sys.excepthook = _tb_info

    logging.info(f'Module: {args.module}')

    if args.kafka:
        logging.info(f'Kafka topic specified: {args.kafka}')

    juris, module = get_jurisdiction(args.module)

    overrides = {}
    overrides.update(getattr(module, 'settings', {}))
    overrides.update(
        {key: value for key, value in vars(args).items() if value is not None}
    )
    with override_settings(settings, overrides):
        report = do_update(args, other, juris)

    stats.close()
    if report.get('success', False):
        return 0
    else:
        return 1


if __name__ == '__main__':
    sys.exit(main())
