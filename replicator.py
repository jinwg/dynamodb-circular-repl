from __future__ import print_function

import os
import re
import time

import concurrent.futures

import boto3

import logging

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

REPLICA_SOURCE_REGION_F = '_repl_source_region'
REPLICA_SOURCE_ACTION_F = '_repl_source_action'

TABLE_STREAM_ARN_REGEX = re.compile("^arn\:aws\:dynamodb\:(.*?)\:.*?\:table\/(.*?)\/stream")


class ReplicatorException(Exception):
    pass


def build_dyn_request_iter(recs, region):
    for r in recs[::-1]:
        op = r['eventName']  # INSERT, MODIFY, REMOVE
        dyn = r['dynamodb']
        if op == 'REMOVE':
            # always replicate deletes
            # a bit dangerous, since an insert followed by a delete may wind up having a delete come back round
            # and kill the new item
            # for a safe delete, suggest check to see if objects match before deleting, but not doing that
            yield (dyn['Keys'], {'DeleteRequest': {'Key': dyn['Keys']}})
        else:
            repl_region = dyn['NewImage'].get(REPLICA_SOURCE_REGION_F)
            if repl_region is not None and os.getenv('TARGET_REGION').lower() in repl_region.values():
                # skip for insert, do not replicate back to the source for new items but modify need to be synced.
                LOGGER.info("Skip replicate back to source for new item.repl_region {0}, targe_region {1}"
                            .format(repl_region,os.getenv('TARGET_REGION')))
                continue
            new_item = dyn['NewImage'].copy()
            if REPLICA_SOURCE_REGION_F not in new_item:
                new_item[REPLICA_SOURCE_REGION_F] = {'S': region}
                new_item[REPLICA_SOURCE_ACTION_F] = {'S': op}
            yield (dyn['Keys'], {'PutRequest': {'Item': new_item}})


def write_dyn_batch(b, t):
    request_list = [x[1] for x in b]
    try:
        session = boto3.session.Session(region_name=os.getenv('TARGET_REGION'))
        c = session.client('dynamodb')
        r = c.batch_write_item(RequestItems={t: request_list})
        unproc = r.get('UnprocessedItems')
        if unproc is not None and len(unproc) > 0:
            # need to rebuild the original key/request structure
            # for splitting unprocessed items into batches
            return [x for x in b if x[1] in unproc]
        return []
    except:
        LOGGER.exception("Error inserting batch")
        # assume all failed
        return b


def split_recs_into_batches(recs):
    # remove duplicate keys
    # go through recs in order
    key_d = {}  # will only store one position
    deduped_recs = []
    for key, req in recs:
        # key contains a dict, so repr
        key_r = repr(key)
        key_r_pos = key_d.get(key_r)
        if key_r_pos is not None:
            # key already in list, pop the old one
            deduped_recs.pop(key_r_pos)
        key_d[key_r] = len(deduped_recs)
        deduped_recs.append((key, req))
    # split into groups of 25
    for i in xrange(0, len(deduped_recs), 25):
        yield deduped_recs[i:i + 25]


def k_seq(x):
    return x['dynamodb'].get('SequenceNumber', 0)


def lambda_handler(event, context):
    recs = [x for x in event['Records'] if 'dynamodb' in x]
    recs.sort(key=lambda x: x['dynamodb']['SequenceNumber'])
    arn=(set([x["eventSourceARN"] for x in recs]))
    if len(arn) !=1:
        raise ReplicatorException("Found events from different tables")
    source_table_arn=arn.pop()
    matches = TABLE_STREAM_ARN_REGEX.match(source_table_arn).groups()
    if matches==None:
        raise ReplicatorException("Unable to parse table and region from ARN: {0}".format(source_table_arn))

    dyn_requests = build_dyn_request_iter(recs, matches[0])
    tstart = time.time()
    try_cnt = 0
    while time.time() - tstart < 245:
        batches = split_recs_into_batches(dyn_requests)
        failures = []
        try_cnt += 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            future_to_batch = {executor.submit(write_dyn_batch, b, matches[1]): b for b in batches}
            for future in concurrent.futures.as_completed(future_to_batch):
                batch = future_to_batch[future]
                failures.extend(future.result())
        if len(failures) == 0:
            break
        LOGGER.info("Failure sending dynamo write batch, waiting and retrying (attempt {0})".format(try_cnt))
        time.sleep(min(5 * try_cnt, 245 - time.time() + tstart))
        dyn_requests = failures
    if len(failures) != 0:
        # we've failed!
        raise ReplicatorException("Unable to handle {0} out of {1} requests".format(len(failures), len(recs)))
    else:
        LOGGER.info("Handled {0} records in {1} sec".format(len(recs), time.time() - tstart))
