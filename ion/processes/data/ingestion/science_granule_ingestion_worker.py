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
            if len(self._cached_times) >= self._checkpoint_frequency:
                self._persist_cache()
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
        cache_size = len(self._cached_times)
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
                start_index = self._add_cache_to_coverage(coverage, timer)
                self._cached_values.clear()
        except:
            raise CorruptionError('failed to write coverage')
        finally:
            try:
                extents = coverage.num_timesteps
                coverage.close()
            except:
                raise CorruptionError('failed to close coverage')
        if debugging:
            timer.complete_step('close')

        log.debug('publishing ingestion event %s, %s, %s', self._dataset_id, start_index, extents)
        self._publisher.publish_event(origin=self._dataset_id, author=self.id, extents=extents, window=(start_index,extents))
        if debugging:
            timer.complete_step('notify')
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
        return start_index

    def _add_timing_stats(self, timer):
        """ add stats from latest coverage operation to Accumulator and periodically log results """
        self._time_stats.add(timer)
        if self._time_stats.get_count('load') % REPORT_FREQUENCY>0:
            return

        if log.isEnabledFor(TRACE):
            # report per step
            for step in 'load', 'lock', 'open', 'combine', 'insert', 'set', 'close', 'notify':
                if step in self._time_stats.count:
                    log.debug('%s step %s times: %s', self.id, step, self._time_stats.to_string(step))
        # report totals
        log.debug('%s total times: %s', self.id, self._time_stats)


