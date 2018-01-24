import argparse
from collections import Counter, OrderedDict
import logging
import queue
import sys
from threading import Lock
import time
import uuid

import numpy

import minknow


class Cache(object):
    def __init__(self, size=100):
        """An ordered dictionary of a maximum size.

        :param size: maximum number of entries, when more entries are added
           the oldest current entries will be removed.

        """

        if size < 1:
            raise AttributeError("'size' must be >1.")
        self.size = size
        self.dict = OrderedDict()
        self.lock = Lock()


    def __getitem__(self, key):
        with self.lock:
            return self.dict[key]


    def __setitem__(self, key, value):
        with self.lock:
            while len(self.dict) >= self.size:
                self.dict.popitem(last=False)
            if key in self.dict:
                del self.dict[key]
            self.dict[key] = value


    def __delitem__(self, key):
        with self.lock:
            del self.dict[key]


    def __len__(self):
        return len(self.dict)


    def popitem(self, last=True):
        """Return the newest (or oldest) entry.

        :param last: if `True` return the newest entry, else the oldest.

        """
        with self.lock:
            return self.dict.popitem(last=last)


    def popitems(self, items, last=True):
        """Return a list of the newest (or oldest) entries.

        :param last: if `True` return the newest entry, else the oldest.

        """
        with self.lock:
            return [self.dict.popitem(last=last) for _ in range(items)]


class ReadUntil(object):
    ALLOWED_MIN_CHUNK_SIZE = 4000

    def __init__(self, mk_rpc_port=8004, cache_size=512):
        """A basic Read Until client.

        :param mk_rpc_port: MinKNOW gRPC port.
        :param cache_size: maximum number of read chunks to cache from
            gRPC stream.

        """
        self.logger = logging.getLogger('ReadUntil')

        self.logger.info('Creating rpc connection on port {}.'.format(mk_rpc_port))
        self.connection = minknow.rpc.Connection(port=mk_rpc_port)
        self.logger.info('Got rpc connection.')
        self.msgs = self.connection.data._pb
        self.device = minknow.Device(self.connection)

        self.signal_dtype = self.device.numpy_data_types.calibrated_signal
        self.cache_size = cache_size

        # the action_queue is used to store unblock/stop_receiving_data
        #    requests before they are put on the gRPC stream.
        self.action_queue = queue.Queue()
        # the data_queue is used to store the latest chunk per channel
        self.data_queue = Cache(size=self.cache_size)


    def run(self, runner_kwargs={'run_time':30}):
        """Run Read Until analysis.

        :param runner_kwargs: kwargs for ._runner() method.

        """
        # .get_live_reads() takes an iterable of requests and generates
        #    raw data chunks and responses to our requests: the iterable
        #    thereby controls the lifetime of the stream. ._runner() as
        #    implemented below initialises the stream then transfers
        #    action requests from the action_queue to the stream.
        reads = self.connection.data.get_live_reads(
            self._runner(**runner_kwargs)
        )

        # ._process_reads() as implemented below is responsible for
        #    placing action requests on the queue and logging the responses
        self._process_reads(reads)

        # reset these for good measure
        self.action_queue = queue.Queue()
        self.data_queue = Cache(size=self.cache_size)
        self.logger.info("Finished run.")


    def aquisition_progress(self):
        """Get MinKNOW data acquisition progress.

        :returns: a structure with attributes .acquired and .processed.

        """
        return self.connection.acquisition.get_progress().raw_per_channel


    def get_read_chunks(self, batch_size=1, last=True):
        """Get a read chunk, removing it from the queue.

        :param batch_size: number of reads.
        :param last: get the most recent (else oldest)?
        """
        return self.data_queue.popitems(items=batch_size, last=True)


    def unblock_read(self, read_channel, read_number):
        self._put_action(read_channel, read_number, 'unblock')


    def stop_receiving_read(self, read_channel, read_number):
        self._put_action(read_channel, read_number, 'stop_further_data')


    def _put_action(self, read_channel, read_number, action):
        """Stores an action requests on the queue ready to be placed on the
        gRPC stream.

        :param read_channel: a read's channel number.
        :param read_number: a read's read number (the nth read per channel).
        :param action: either 'stop_further_data' or 'unblock'.

        """
        #TODO: refactor this to allow placing multiple actions simultaneously
        action_id = str(uuid.uuid4())
        action_kwargs = {
            'action_id': action_id,
            'channel': read_channel,
            'number': read_number,
        }
        if action == 'stop_further_data':
            action_kwargs[action] = self.msgs.GetLiveReadsRequest.StopFurtherData()
        elif action == 'unblock':
            action_kwargs[action] = self.msgs.GetLiveReadsRequest.UnblockAction()
        else:
            raise ValueError("'action' parameter must must be 'stop_further_data' or 'unblock'.")
        
        action_request = self.msgs.GetLiveReadsRequest.Action(**action_kwargs)
        action_group = self.msgs.GetLiveReadsRequest(
            actions=self.msgs.GetLiveReadsRequest.Actions(actions=[action_request])
        )
        self.action_queue.put(action_group)
        self.logger.debug('Action {} on channel {}, read {} : {}'.format(
            action_id, read_channel, read_number, action
        ))


    def _runner(self, run_time, first_channel=1, last_channel=512, min_chunk_size=1000):
        """Yield the stream initializer request followed by action requests
        placed into the action_queue.


        :param run_time: maximum time for which to yield actions.
        :param first_channel: lowest channel for which to receive raw data.
        :param last_channel: highest channel (inclusive) for which to receive data.
        :param min_chunk_size: minimum number of raw samples in an raw data chunk.
        """
        timeout_pt = time.time() + run_time

        if min_chunk_size > self.ALLOWED_MIN_CHUNK_SIZE:
            self.logger.warning("Reducing min_chunk_size to {}".format(self.ALLOWED_MIN_CHUNK_SIZE))
            min_chunk_size = self.ALLOWED_MIN_CHUNK_SIZE

        self.logger.info("Sending init command")
        yield self.msgs.GetLiveReadsRequest(
            setup=self.msgs.GetLiveReadsRequest.StreamSetup(
                first_channel=first_channel,
                last_channel=last_channel,
                raw_data_type=self.msgs.GetLiveReadsRequest.CALIBRATED,
                sample_minimum_chunk_size=min_chunk_size
            )
        )

        self.logger.info("Running Read Until for {} seconds.".format(run_time))
        while time.time() < timeout_pt:
            try:
                action = self.action_queue.get()
            except queue.Empty:
                continue
            else:
                yield action

        self.logger.info("Stream finished after timeout.")


    def _process_reads(self, reads):
        """Process the gRPC stream data, storing read chunks in the data_queue.

        :param reads: gRPC data stream iterable as produced by get_live_reads().
        
        """
        response_counter = Counter()

        unique_reads = set()

        read_count = 0
        samples_behind = 0
        raw_data_bytes = 0
        last_msg_time = time.time()
        for reads_chunk in reads:

            # In each iteration, we get:
            #   i) responses to our previous actions (success/fail)
            #  ii) raw data for current reads

            # record a count of success and fails            
            if len(reads_chunk.action_reponses):
                for response in reads_chunk.action_reponses:
                    response_counter[response.response] += 1

            progress = self.aquisition_progress()
            for read_channel in reads_chunk.channels:
                read_count += 1
                read = reads_chunk.channels[read_channel]
                unique_reads.add(read.id)
                read_samples_behind = progress.acquired - read.chunk_start_sample
                samples_behind += read_samples_behind
                raw_data_bytes += len(read.raw_data)

                self.data_queue[read_channel] = read

            now = time.time()
            if last_msg_time + 1 < now:
                self.logger.info(
                    "Interval update: {} read sections, {} unique reads (ever), "
                    "average {:.0f} samples behind. {:.2f} MB raw data."
                    .format(
                        read_count, len(unique_reads),
                        samples_behind/read_count, raw_data_bytes/1024/1024
                    )
                )
                self.logger.info("Response summary: {}".format(response_counter))

                read_count = 0
                samples_behind = 0
                raw_data_bytes = 0
                last_msg_time = now


def _get_parser():
    parser = argparse.ArgumentParser('Read until with alignment filter.')
    parser.add_argument('--port', type=int, default=8004,
        help='MinKNOW server gRPC port.')
    parser.add_argument('--analysis_delay', type=int, default=1,
        help='Period to wait before starting analysis.')
    parser.add_argument(
        '--debug', help="Print all debugging information",
        action="store_const", dest="log_level",
        const=logging.DEBUG, default=logging.WARNING,
    )
    parser.add_argument(
        '--verbose', help="Print verbose messaging.",
        action="store_const", dest="log_level",
        const=logging.INFO,
    )
    return parser


def simple_analysis(client, batch_size=10, timeout=2, delay=1, throttle=0.1):
    logger = logging.getLogger('Analysis')
    logger.info('Starting analysis of reads in {}s.'.format(delay))
    time.sleep(delay)

    last_good = time.time()
    while True:
        try:
            read_batch = client.get_read_chunks(batch_size=batch_size, last=True)
        except KeyError as e:
            if last_good + timeout < time.time():
                logger.critical('Aborting analysis after {}s of inactivity.'.format(timeout))
                break
        except Exception as e:
            raise e
        else:
            last_good = time.time()
            for channel, read in read_batch:
                # convert the read data into a numpy array of correct type
                raw_data = numpy.fromstring(read.raw_data, client.signal_dtype)
                read.raw_data = bytes('', 'utf-8') # we don't need this now
                if read.median_before > read.median and (read.median_before - read.median) > 60:
                    client.stop_receiving_read(channel, read.number)
        time.sleep(throttle)
 
    logger.info('Finished analysis of reads.')


def main():
    import concurrent.futures
    args = _get_parser().parse_args() 

    logging.basicConfig(format='[%(asctime)s - %(name)s] %(message)s',
        datefmt='%H:%M:%S', level=args.log_level)

    read_until_client = ReadUntil(mk_rpc_port=args.port)
    futures = list()
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures.append(executor.submit(read_until_client.run))
        futures.append(executor.submit(simple_analysis, read_until_client, delay=args.analysis_delay))

    for f in concurrent.futures.as_completed(futures):
        if f.exception() is not None:
            raise f.exception()


if __name__ == "__main__":
    main()

