#!/usr/bin/env python
'''
@author Luke Campbell <LCampbell@ASAScience.com>
@file ion/processes/data/ingestion/science_granule_ingestion_worker.py
@date 06/26/12 11:38
@description Ingestion Process
'''
from ion.services.dm.utility.granule.record_dictionary import RecordDictionaryTool
from interface.services.coi.iresource_registry_service import ResourceRegistryServiceClient
from pyon.core.exception import CorruptionError
from pyon.event.event import handle_stream_exception, EventPublisher
from pyon.public import log, RT, PRED, CFG, OT
from ion.services.dm.inventory.dataset_management_service import DatasetManagementService
from interface.objects import Granule
from ion.core.process.transform import TransformStreamListener
from ion.util.time_utils import TimeUtils

from ooi.timer import Timer, Accumulator
from ooi.logging import TRACE
from logging import DEBUG
from threading import Lock

import collections
import gevent
import time
import uuid


REPORT_FREQUENCY=100
MAX_RETRY_TIME=3600
TIMESTAMP_KEY = 'ingestion_timestamp'
class ScienceGranuleIngestionWorker(TransformStreamListener):
    CACHE_LIMIT=CFG.get_safe('container.ingestion_cache',5)

    def __init__(self, *args,**kwargs):
        super(ScienceGranuleIngestionWorker, self).__init__(*args, **kwargs)

        # cache values from multiple granules in dict { 'field': [ value, value, ... ], ... }
        self._cached_values = {}
        # cache times in number format -- convert to field type after we create coverage and see if they are needed
        self._cached_times = []
        # persist cache when list size >= this value (count of values, not count of granules)
        self._checkpoint_frequency = 20

        self._stream_id = None
        self._lock = Lock()
        self._time_stats = Accumulator(format='%3f')

    def on_start(self): #pragma no cover
        super(ScienceGranuleIngestionWorker,self).on_start()
        self._publisher = EventPublisher(OT.DatasetModified)
        self._running = True

    def on_quit(self): #pragma no cover
        self._running = False
        self._persist_cache()
        super(ScienceGranuleIngestionWorker, self).on_quit()

    def recv_packet(self, msg, stream_route, stream_id):
        ''' receive packet for ingestion '''
        log.trace('received granule for stream %s', stream_id)
        try:
            self._validate_stream(stream_id)
            self._add_to_cache(msg)
            # slightly hacky -- want len of the arrays in the dict, should always have the
            if len(self._cached_values[TIMESTAMP_KEY]) >= self._checkpoint_frequency:
                self._persist_cache()
                self._publisher.publish_event(origin=dataset_id, author=self.id, extents=extents, window=window)
        except:
            log.error('failed to ingest granule', exc_info=True)
            raise

    def _validate_stream(self, stream_id):
        if not self._stream_id:
            self._stream_id = stream_id
            rr_client = ResourceRegistryServiceClient()
            ids, _ = rr_client.find_subjects(subject_type=RT.Dataset,predicate=PRED.hasStream,object=stream_id,id_only=True)
            if not ids:
                raise Exception('no dataset found for stream %s'%stream_id)
            self._dataset_id = ids[0]
            if log.isEnabledFor(DEBUG):
                path = DatasetManagementService._get_coverage_path(self._dataset_id)
                log.debug('%s: init stream %s, dataset %s, coverage %s', self.id, stream_id, self._dataset_id, path)
        elif self._stream_id != stream_id:
            raise Exception('expected stream %s, received granule from stream %s'%(self._stream_id, stream_id))

    def _add_to_cache(self, msg):
        """ extract values that will be needed to write coverage and save for later persistence """
        debugging = log.isEnabledFor(DEBUG)
        if debugging:
            timer = Timer('cache')

        if not msg:
            log.error('Received empty message')
            return
        if not isinstance(msg, Granule):
            log.error('Ingestion received a message that is not a granule: %s', msg)
            return
        rdt = RecordDictionaryTool.load_from_granule(msg)
        if rdt is None:
            log.error('Invalid granule (no RDT) for stream %s', self._stream_id)
            return
        if debugging:
            timer.complete_step('load')

        # for all fields we've already seen, add values to lists in cache
        granule_size = len(rdt)
        for known_key in self._cached_values.iterkeys():
            if known_key in rdt:
                self._cached_values[known_key] += rdt[known_key]
            else:
                self._cached_values[known_key] += [None]*granule_size
        # if there are any new fields, update cache with nulls then add current granule values
        cache_size = len(self._cached_values[TIMESTAMP_KEY])
        for granule_key in rdt.iterkeys():
            if granule_key not in self._cached_values:
                self._cached_values[granule_key] = [None]*cache_size + rdt[granule_key]
        # add timestamps
        time = time.time()
        self._cached_times += [time]*granule_size

    def _persist_cache(self):
        """ write cached granules to disk """
        debugging = log.isEnabledFor(DEBUG)
        timer = Timer('persist') if debugging else None
        if debugging:
            path = DatasetManagementService._get_coverage_path(self._dataset_id)
            log.debug('%s: add_granule stream %s dataset %s file %s',
                self.id, self._stream_id, self._dataset_id, path)

        coverage = DatasetManagementService._get_coverage(self._dataset_id, mode='a')
        if debugging:
            timer.complete_step('open')
        try:
            with self._lock:
                self._add_cache_to_coverage(coverage, timer)
                self._cached_values.clear()
        except:
            raise CorruptionError('failed to write coverage')
        finally:
            try:
                coverage.close()
            except:
                raise CorruptionError('failed to close coverage')
        if debugging:
            timer.complete_step('close')
            self._add_timing_stats(timer)

    def _add_cache_to_coverage(self, coverage, timer):
        size = len(self._cached_times)
        coverage.insert_timesteps(size, oob=False)
        if timer:
            timer.complete_step('insert')

        start_index = coverage.num_timesteps - size
        slice_ = slice(start_index, None)
        for k,v in self._cached_values.iteritems():
            coverage.set_parameter_values(param_name=k, tdoa=slice_, value=v)
        if TIMESTAMP_KEY in coverage.list_parameters():
            timestamps = []
            last_time = None
            for t in self._cached_times:
                if t != last_time:
                    value = TimeUtils.ts_to_units(coverage.get_parameter_context(TIMESTAMP_KEY).uom, t)
                    last_time = t
                timestamps.append(value)
            coverage.set_parameter_values(param_name=TIMESTAMP_KEY, tdoa=slice_, value=timestamps)
        if timer:
            timer.complete_step('set')

############################################################################
#
#    def _new_dataset(self, stream_id):
#        '''
#        Adds a new dataset to the internal cache of the ingestion worker
#        '''
#        rr_client = ResourceRegistryServiceClient()
#        datasets, _ = rr_client.find_subjects(subject_type=RT.Dataset,predicate=PRED.hasStream,object=stream_id,id_only=True)
#        if datasets:
#            return datasets[0]
#        return None
#
#    def get_dataset(self,stream_id):
#        '''
#        Memoization (LRU) of _new_dataset
#        '''
#        try:
#            result = self._datasets.pop(stream_id)
#        except KeyError:
#            result = self._new_dataset(stream_id)
#            if result is None:
#                return None
#            if len(self._datasets) >= self.CACHE_LIMIT:
#                self._datasets.popitem(0)
#        self._datasets[stream_id] = result
#        return result
#
#    def get_coverage(self, stream_id):
#        '''
#        Memoization (LRU) of _get_coverage
#        '''
#        try:
#            result = self._coverages.pop(stream_id)
#        except KeyError:
#            dataset_id = self.get_dataset(stream_id)
#            if dataset_id is None:
#                return None
#            result = DatasetManagementService._get_coverage(dataset_id, mode='a')
#            if result is None:
#                return None
#            if len(self._coverages) >= self.CACHE_LIMIT:
#                k, coverage = self._coverages.popitem(0)
#                coverage.close(timeout=5)
#        self._coverages[stream_id] = result
#        return result
#
#    def dataset_changed(self, dataset_id, extents, window):
#        self._publisher.publish_event(origin=dataset_id, author=self.id, extents=extents, window=window)
#
#    @handle_stream_exception()
#    def xxrecv_packet(self, msg, stream_route, stream_id):
#        ''' receive packet for ingestion '''
#        log.trace('received granule for stream %s', stream_id)
#
#        if msg == {}:
#            log.error('Received empty message from stream: %s', stream_id)
#            return
#        # Message validation
#        if not isinstance(msg, Granule):
#            log.error('Ingestion received a message that is not a granule: %s', msg)
#            return
#
#        rdt = RecordDictionaryTool.load_from_granule(msg)
#        if rdt is None:
#            log.error('Invalid granule (no RDT) for stream %s', stream_id)
#            return
#        if not len(rdt):
#            log.debug('Empty granule for stream %s', stream_id)
#            return
#
#        self.persist_or_timeout(stream_id, rdt)
#
#    def persist_or_timeout(self, stream_id, rdt):
#        """ retry writing coverage multiple times and eventually time out """
#        done = False
#        timeout = 2
#        start = time.time()
#        while not done:
#            try:
#                self.add_granule(stream_id, rdt)
#                done = True
#            except:
#                log.exception('An issue with coverage, retrying after a bit')
#                if (time.time() - start) > MAX_RETRY_TIME: # After an hour just give up
#                    dataset_id = self.get_dataset(stream_id)
#                    log.error("We're giving up, the coverage needs to be inspected %s", DatasetManagementService._get_coverage_path(dataset_id))
#                    raise
#
#                if stream_id in self._coverages:
#                    log.info('Popping coverage for stream %s', stream_id)
#                    self._coverages.pop(stream_id)
#
#                gevent.sleep(timeout)
#                if timeout > (60 * 5):
#                    timeout = 60 * 5
#                else:
#                    timeout *= 2
#
#    def add_granule(self,stream_id, rdt):
#        ''' Appends the granule's data to the coverage and persists it. '''
#        debugging = log.isEnabledFor(DEBUG)
#        timer = Timer() if debugging else None
#        if stream_id in self._bad_coverages:
#            log.info('Message attempting to be inserted into bad coverage: %s',
#                     DatasetManagementService._get_coverage_path(self.get_dataset(stream_id)))
#
#
#        #--------------------------------------------------------------------------------
#        # Coverage determiniation and appending
#        #--------------------------------------------------------------------------------
#        dataset_id = self.get_dataset(stream_id)
#        if not dataset_id:
#            log.error('No dataset could be determined on this stream: %s', stream_id)
#            return
#        try:
#            coverage = self.get_coverage(stream_id)
#        except IOError as e:
#            log.error("Couldn't open coverage: %s",
#                      DatasetManagementService._get_coverage_path(self.get_dataset(stream_id)))
#            raise CorruptionError(e.message)
#
#        if debugging:
#            path = DatasetManagementService._get_coverage_path(dataset_id)
#            log.debug('%s: add_granule stream %s dataset %s coverage %r file %s',
#                      self.id, stream_id, dataset_id, coverage, path)
#
#        if not coverage:
#            log.error('Could not persist coverage from granule, coverage is None')
#            return
#        #--------------------------------------------------------------------------------
#        # Actual persistence
#        #--------------------------------------------------------------------------------
#        elements = len(rdt)
#        if debugging:
#            timer.complete_step('checks') # lightweight ops, should be zero
#        try:
#            coverage.insert_timesteps(elements, oob=False)
#        except IOError as e:
#            log.error("Couldn't insert time steps for coverage: %s",
#                      DatasetManagementService._get_coverage_path(self.get_dataset(stream_id)), exc_info=True)
#            try:
#                coverage.close()
#            finally:
#                self._bad_coverages[stream_id] = 1
#                raise CorruptionError(e.message)
#        if debugging:
#            timer.complete_step('insert')
#
#        start_index = coverage.num_timesteps - elements
#
#        for k,v in rdt.iteritems():
#            slice_ = slice(start_index, None)
#            try:
#                coverage.set_parameter_values(param_name=k, tdoa=slice_, value=v)
#            except IOError as e:
#                log.error("Couldn't insert values for coverage: %s",
#                          DatasetManagementService._get_coverage_path(self.get_dataset(stream_id)), exc_info=True)
#                try:
#                    coverage.close()
#                finally:
#                    self._bad_coverages[stream_id] = 1
#                    raise CorruptionError(e.message)
#        if 'ingestion_timestamp' in coverage.list_parameters():
#            t_now = time.time()
#            ntp_time = TimeUtils.ts_to_units(coverage.get_parameter_context('ingestion_timestamp').uom, t_now)
#            coverage.set_parameter_values(param_name='ingestion_timestamp', tdoa=slice_, value=ntp_time)
#        if debugging:
#            timer.complete_step('keys')
#        DatasetManagementService._save_coverage(coverage)
#        if debugging:
#            timer.complete_step('save')
#        self.dataset_changed(dataset_id,coverage.num_timesteps,(start_index,start_index+elements))
#        if debugging:
#            timer.complete_step('notify')
#            self._add_timing_stats(timer)

    def _add_timing_stats(self, timer):
        """ add stats from latest coverage operation to Accumulator and periodically log results """
        self._time_stats.add(timer)
        if self._time_stats.get_count('load') % REPORT_FREQUENCY>0:
            return

        if log.isEnabledFor(TRACE):
            # report per step
            for step in 'load', 'lock', 'open', 'combine', 'insert', 'set', 'close':
                if step in self._time_stats.count:
                    log.debug('%s step %s times: %s', self.id, step, self._time_stats.to_string(step))
        # report totals
        log.debug('%s total times: %s', self.id, self._time_stats)


