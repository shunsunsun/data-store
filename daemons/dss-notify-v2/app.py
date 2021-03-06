"""
See Readme.md in this directory for documentation on the dss-notify-v2 daemon.

storage_event -> invoke_notify_daemon -> invoke_sfn -> sfn_dynamodb_loop -> sqs -> invoke_notify_daemon
"""

import os, sys, json, logging
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor

import domovoi

pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), 'domovoilib'))  # noqa
sys.path.insert(0, pkg_root)  # noqa

import dss
from dss import Config, Replica
from dss.logging import configure_lambda_logging
from dss.storage.bundles import get_tombstoned_bundles
from dss.storage.identifiers import TOMBSTONE_SUFFIX
from dss.events import (get_bundle_metadata_document, get_deleted_bundle_metadata_document, record_event_for_bundle,
                        delete_event_for_bundle, build_bundle_metadata_document)
from dss.events.handlers.notify_v2 import should_notify, notify_or_queue, notify

from dss.events.handlers.sync import exists
from dss.subscriptions_v2 import get_subscription, get_subscriptions_for_replica


configure_lambda_logging()
logger = logging.getLogger(__name__)
dss.Config.set_config(dss.BucketConfig.NORMAL)

app = domovoi.Domovoi()

# This entry point is for S3 native events forwarded through SQS.
@app.s3_event_handler(
    bucket=Config.get_s3_bucket(),
    events=["s3:ObjectCreated:*", "s3:ObjectRemoved:Delete"],
    use_sqs=True,
    sqs_queue_attributes=dict(VisibilityTimeout="920"),  # Lambda timeout + 20 seconds
)
def launch_from_s3_event(event, context):
    replica = Replica.aws
    if event.get("Event") == "s3:TestEvent":
        logger.info("S3 test event received and processed successfully")
    else:
        for event_record in event['Records']:
            bucket = event_record['s3']['bucket']['name']
            if bucket != replica.bucket:
                logger.error("Received S3 event for bucket %s with no configured replica", bucket)
                continue
            key = unquote(event_record['s3']['object']['key'])
            if key.startswith("bundles"):
                is_delete_event = (event_record['eventName'] == "ObjectRemoved:Delete")
                _handle_bucket_event(replica, key, is_delete_event)
            else:
                logger.warning(f"Notifications not supported for {key}")

# This entry point is for external events forwarded by dss-gs-event-relay (or other event sources) through SNS-SQS.
@app.sqs_queue_subscriber(
    "dss-notify-v2-event-relay-" + os.environ['DSS_DEPLOYMENT_STAGE'],
    queue_attributes=dict(VisibilityTimeout="920"),  # Lambda timeout + 20 seconds
)
def launch_from_forwarded_event(event, context):
    replica = Replica.gcp
    for event_record in event['Records']:
        message = json.loads(json.loads(event_record['body'])['Message'])
        if message['selfLink'].startswith("https://www.googleapis.com/storage"):
            key = message['name']
            if key.startswith("bundles"):
                is_delete_event = (message['resourceState'] == "not_exists")
                _handle_bucket_event(replica, key, is_delete_event)
            else:
                logger.warning(f"Notifications not supported for {key}")
        else:
            raise NotImplementedError()

# This entry point is for queued notifications for manual notification or redrive
@app.sqs_queue_subscriber(
    "dss-notify-v2-" + os.environ['DSS_DEPLOYMENT_STAGE'],
    batch_size=1,
    queue_attributes=dict(
        VisibilityTimeout=str(6 * 3600),  # Retry every six hour
        MessageRetentionPeriod=str(3 * 24 * 3600)  # Retain messages for 7 days
    )
)
def launch_from_notification_queue(event, context):
    for event_record in event['Records']:
        message = json.loads(event_record['body'])
        replica = Replica[message['replica']]
        owner = message['owner']
        uuid = message['uuid']
        key = message['key']
        event_type = message['event_type']
        subscription = get_subscription(replica, owner, uuid)
        if subscription is not None:
            if "DELETE" == event_type:
                metadata_document = get_deleted_bundle_metadata_document(replica, key)
            else:
                if not exists(replica, key):
                    logger.warning(f"Key %s not found in replica %s, unable to notify %s", key, replica.name, uuid)
                    return
                metadata_document = get_bundle_metadata_document(replica, key)
            if not notify(subscription, metadata_document, key):
                # Erroring causes the message to remain in the queue
                raise DSSFailedNotificationDelivery()
        else:
            logger.warning(f"Recieved queue message with no matching subscription:{message}")

def _handle_bucket_event(replica: Replica, key: str, is_delete_event: bool):
    if is_delete_event:
        metadata_document = get_deleted_bundle_metadata_document(replica, key)
    else:
        if exists(replica, key):
            if key.endswith(TOMBSTONE_SUFFIX):
                for zombie_key in get_tombstoned_bundles(replica, key):
                    delete_event_for_bundle(replica, zombie_key)
                metadata_document = build_bundle_metadata_document(replica, key)
            else:
                metadata_document = record_event_for_bundle(replica, key)
        else:
            logger.error(json.dumps(dict(message="Key not found", replica=replica.name, key=key), indent=4))
            return

    _deliver_notifications(replica, metadata_document, key)

def _deliver_notifications(replica: Replica, metadata_document: dict, key: str):
    def _func(subscription):
        if should_notify(replica, subscription, metadata_document, key):
            notify_or_queue(replica, subscription, metadata_document, key)

    # TODO: Consider scaling parallelization with Lambda size
    logger.info(json.dumps(dict(message="Attempting notifications",
                                replica=replica.name,
                                key=key,
                                event_type=metadata_document['event_type']), indent=4))
    with ThreadPoolExecutor(max_workers=20) as e:
        e.map(_func, get_subscriptions_for_replica(replica))

class DSSFailedNotificationDelivery(Exception):
    pass
