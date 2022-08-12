#!/usr/bin/env python
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0OA
#
# Authors:
# - Wen Guan, <wen.guan@cern.ch>, 2019 - 202

import copy
import datetime
import time
import traceback

from idds.common import exceptions
from idds.common.constants import (Sections, TransformStatus, TransformLocking, TransformType,
                                   ContentRelationType, CollectionStatus,
                                   ContentType, ContentStatus,
                                   ProcessingStatus, MessageType, MessageTypeStr,
                                   MessageStatus, MessageSource, MessageDestination)
from idds.common.utils import setup_logging
from idds.core import (transforms as core_transforms,
                       processings as core_processings,
                       catalog as core_catalog)
from idds.agents.common.baseagent import BaseAgent
from idds.agents.common.eventbus.event import (NewTransformEvent,
                                               UpdateTransformEvent,
                                               AbortTransformEvent,
                                               ResumeTransformEvent,
                                               AbortProcessingEvent,
                                               ResumeProcessingEvent,
                                               UpdateRequestEvent,
                                               NewProcessingEvent,
                                               UpdateProcessingEvent)

setup_logging(__name__)


class Transformer(BaseAgent):
    """
    Transformer works to process transforms.
    """

    def __init__(self, num_threads=1, poll_time_period=1800, retrieve_bulk_size=10,
                 message_bulk_size=10000, **kwargs):
        super(Transformer, self).__init__(num_threads=num_threads, **kwargs)
        self.config_section = Sections.Transformer
        self.poll_time_period = int(poll_time_period)
        self.retrieve_bulk_size = int(retrieve_bulk_size)
        self.message_bulk_size = int(message_bulk_size)

        if not hasattr(self, 'retries') or not self.retries:
            self.retries = 100
        else:
            self.retries = int(self.retries)

        self.number_workers = 0
        if not hasattr(self, 'max_number_workers') or not self.max_number_workers:
            self.max_number_workers = 3
        else:
            self.max_number_workers = int(self.max_number_workers)

    def is_ok_to_run_more_requests(self):
        if self.number_workers >= self.max_number_workers:
            return False
        return True

    def show_queue_size(self):
        q_str = "number of transforms: %s, max number of transforms: %s" % (self.number_workers, self.max_number_workers)
        self.logger.debug(q_str)

    def get_new_transforms(self):
        """
        Get new transforms to process
        """
        try:
            if not self.is_ok_to_run_more_requests():
                return []

            self.show_queue_size()

            transform_status = [TransformStatus.New, TransformStatus.Ready, TransformStatus.Extend]
            next_poll_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_time_period)
            transforms_new = core_transforms.get_transforms_by_status(status=transform_status, locking=True,
                                                                      not_lock=True, next_poll_at=next_poll_at,
                                                                      bulk_size=self.retrieve_bulk_size)

            self.logger.debug("Main thread get %s New+Ready+Extend transforms to process" % len(transforms_new))
            if transforms_new:
                self.logger.info("Main thread get %s New+Ready+Extend transforms to process" % len(transforms_new))

            for tf in transforms_new:
                event = NewTransformEvent(publisher_id=self.id, transform_id=tf.transform_id)
                self.event_bus.send(event)

            return transforms_new
        except exceptions.DatabaseException as ex:
            if 'ORA-00060' in str(ex):
                self.logger.warn("(cx_Oracle.DatabaseError) ORA-00060: deadlock detected while waiting for resource")
            else:
                # raise ex
                self.logger.error(ex)
                self.logger.error(traceback.format_exc())
        return []

    def get_running_transforms(self):
        """
        Get running transforms
        """
        try:
            if not self.is_ok_to_run_more_requests():
                return []

            self.show_queue_size()

            transform_status = [TransformStatus.Transforming,
                                TransformStatus.ToCancel, TransformStatus.Cancelling,
                                TransformStatus.ToSuspend, TransformStatus.Suspending,
                                TransformStatus.ToExpire, TransformStatus.Expiring,
                                TransformStatus.ToResume, TransformStatus.Resuming,
                                TransformStatus.ToFinish, TransformStatus.ToForceFinish]
            next_poll_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_time_period)
            transforms = core_transforms.get_transforms_by_status(status=transform_status,
                                                                  period=None,
                                                                  locking=True,
                                                                  not_lock=True, next_poll_at=next_poll_at,
                                                                  bulk_size=self.retrieve_bulk_size)

            self.logger.debug("Main thread get %s transforming transforms to process" % len(transforms))
            if transforms:
                self.logger.info("Main thread get %s transforming transforms to process" % len(transforms))

            for tf in transforms:
                event = UpdateTransformEvent(publisher_id=self.id, transform_id=tf.transform_id)
                self.event_bus.send(event)

            return transforms
        except exceptions.DatabaseException as ex:
            if 'ORA-00060' in str(ex):
                self.logger.warn("(cx_Oracle.DatabaseError) ORA-00060: deadlock detected while waiting for resource")
            else:
                self.logger.error(ex)
                self.logger.error(traceback.format_exc())
        return []

    def get_request(self, transform_id, status=None, locking=False):
        try:
            return core_transforms.get_transform_by_id_status(transfrom_id=transform_id, status=status, locking=locking)
        except exceptions.DatabaseException as ex:
            if 'ORA-00060' in str(ex):
                self.logger.warn("(cx_Oracle.DatabaseError) ORA-00060: deadlock detected while waiting for resource")
            else:
                # raise ex
                self.logger.error(ex)
                self.logger.error(traceback.format_exc())
        return None

    def get_new_contents(self, transform, new_input_output_maps):
        new_input_contents, new_output_contents, new_log_contents = [], [], []
        new_input_dependency_contents = []
        for map_id in new_input_output_maps:
            inputs = new_input_output_maps[map_id]['inputs'] if 'inputs' in new_input_output_maps[map_id] else []
            inputs_dependency = new_input_output_maps[map_id]['inputs_dependency'] if 'inputs_dependency' in new_input_output_maps[map_id] else []
            outputs = new_input_output_maps[map_id]['outputs'] if 'outputs' in new_input_output_maps[map_id] else []
            logs = new_input_output_maps[map_id]['logs'] if 'logs' in new_input_output_maps[map_id] else []

            for input_content in inputs:
                content = {'transform_id': transform['transform_id'],
                           'coll_id': input_content['coll_id'],
                           'request_id': transform['request_id'],
                           'workload_id': transform['workload_id'],
                           'map_id': map_id,
                           'scope': input_content['scope'],
                           'name': input_content['name'],
                           'min_id': input_content['min_id'] if 'min_id' in input_content else 0,
                           'max_id': input_content['max_id'] if 'max_id' in input_content else 0,
                           'status': input_content['status'] if 'status' in input_content and input_content['status'] is not None else ContentStatus.New,
                           'substatus': input_content['substatus'] if 'substatus' in input_content and input_content['substatus'] is not None else ContentStatus.New,
                           'path': input_content['path'] if 'path' in input_content else None,
                           'content_type': input_content['content_type'] if 'content_type' in input_content else ContentType.File,
                           'content_relation_type': ContentRelationType.Input,
                           'bytes': input_content['bytes'],
                           'adler32': input_content['adler32'],
                           'content_metadata': input_content['content_metadata']}
                if content['min_id'] is None:
                    content['min_id'] = 0
                if content['max_id'] is None:
                    content['max_id'] = 0
                new_input_contents.append(content)
            for input_content in inputs_dependency:
                content = {'transform_id': transform['transform_id'],
                           'coll_id': input_content['coll_id'],
                           'request_id': transform['request_id'],
                           'workload_id': transform['workload_id'],
                           'map_id': map_id,
                           'scope': input_content['scope'],
                           'name': input_content['name'],
                           'min_id': input_content['min_id'] if 'min_id' in input_content else 0,
                           'max_id': input_content['max_id'] if 'max_id' in input_content else 0,
                           'status': input_content['status'] if 'status' in input_content and input_content['status'] is not None else ContentStatus.New,
                           'substatus': input_content['substatus'] if 'substatus' in input_content and input_content['substatus'] is not None else ContentStatus.New,
                           'path': input_content['path'] if 'path' in input_content else None,
                           'content_type': input_content['content_type'] if 'content_type' in input_content else ContentType.File,
                           'content_relation_type': ContentRelationType.InputDependency,
                           'bytes': input_content['bytes'],
                           'adler32': input_content['adler32'],
                           'content_metadata': input_content['content_metadata']}
                if content['min_id'] is None:
                    content['min_id'] = 0
                if content['max_id'] is None:
                    content['max_id'] = 0
                new_input_dependency_contents.append(content)
            for output_content in outputs:
                content = {'transform_id': transform['transform_id'],
                           'coll_id': output_content['coll_id'],
                           'request_id': transform['request_id'],
                           'workload_id': transform['workload_id'],
                           'map_id': map_id,
                           'scope': output_content['scope'],
                           'name': output_content['name'],
                           'min_id': output_content['min_id'] if 'min_id' in output_content else 0,
                           'max_id': output_content['max_id'] if 'max_id' in output_content else 0,
                           'status': ContentStatus.New,
                           'substatus': ContentStatus.New,
                           'path': output_content['path'] if 'path' in output_content else None,
                           'content_type': output_content['content_type'] if 'content_type' in output_content else ContentType.File,
                           'content_relation_type': ContentRelationType.Output,
                           'bytes': output_content['bytes'],
                           'adler32': output_content['adler32'],
                           'content_metadata': output_content['content_metadata']}
                if content['min_id'] is None:
                    content['min_id'] = 0
                if content['max_id'] is None:
                    content['max_id'] = 0
                new_output_contents.append(content)
            for log_content in logs:
                content = {'transform_id': transform['transform_id'],
                           'coll_id': log_content['coll_id'],
                           'request_id': transform['request_id'],
                           'workload_id': transform['workload_id'],
                           'map_id': map_id,
                           'scope': log_content['scope'],
                           'name': log_content['name'],
                           'min_id': log_content['min_id'] if 'min_id' in log_content else 0,
                           'max_id': log_content['max_id'] if 'max_id' in log_content else 0,
                           'status': ContentStatus.New,
                           'substatus': ContentStatus.New,
                           'path': log_content['path'] if 'path' in log_content else None,
                           'content_type': log_content['content_type'] if 'content_type' in log_content else ContentType.File,
                           'content_relation_type': ContentRelationType.Log,
                           'bytes': log_content['bytes'],
                           'adler32': log_content['adler32'],
                           'content_metadata': log_content['content_metadata']}
                if content['min_id'] is None:
                    content['min_id'] = 0
                if content['max_id'] is None:
                    content['max_id'] = 0
                new_output_contents.append(content)
        return new_input_contents, new_output_contents, new_log_contents, new_input_dependency_contents

    def is_all_inputs_dependency_available(self, inputs_dependency):
        for content in inputs_dependency:
            if content['status'] not in [ContentStatus.Available, ContentStatus.FakeAvailable]:
                return False
        return True

    def is_all_inputs_dependency_terminated(self, inputs_dependency):
        for content in inputs_dependency:
            if content['status'] not in [ContentStatus.Available, ContentStatus.FakeAvailable,
                                         ContentStatus.FinalFailed, ContentStatus.Missing]:
                return False
        return True

    def is_input_dependency_terminated(self, input_dependency):
        if input_dependency['status'] in [ContentStatus.Available, ContentStatus.FakeAvailable,
                                          ContentStatus.FinalFailed, ContentStatus.Missing]:
            return True
        return False

    def get_updated_contents(self, transform, registered_input_output_maps):
        updated_contents = []
        updated_input_contents_full, updated_output_contents_full = [], []

        for map_id in registered_input_output_maps:
            inputs = registered_input_output_maps[map_id]['inputs'] if 'inputs' in registered_input_output_maps[map_id] else []
            outputs = registered_input_output_maps[map_id]['outputs'] if 'outputs' in registered_input_output_maps[map_id] else []
            inputs_dependency = registered_input_output_maps[map_id]['inputs_dependency'] if 'inputs_dependency' in registered_input_output_maps[map_id] else []

            if self.is_all_inputs_dependency_available(inputs_dependency):
                # self.logger.debug("all input dependency available: %s, inputs: %s" % (str(inputs_dependency), str(inputs)))
                for content in inputs:
                    content['substatus'] = ContentStatus.Available
                    if content['status'] != content['substatus']:
                        updated_content = {'content_id': content['content_id'],
                                           'status': content['substatus'],
                                           'substatus': content['substatus']}
                        content['status'] = content['substatus']
                        updated_contents.append(updated_content)
                        updated_input_contents_full.append(content)
            elif self.is_all_inputs_dependency_terminated(inputs_dependency):
                # self.logger.debug("all input dependency terminated: %s, inputs: %s, outputs: %s" % (str(inputs_dependency), str(inputs), str(outputs)))
                for content in inputs:
                    content['substatus'] = ContentStatus.Missing
                    if content['status'] != content['substatus']:
                        updated_content = {'content_id': content['content_id'],
                                           'status': content['substatus'],
                                           'substatus': content['substatus']}
                        content['status'] = content['substatus']
                        updated_contents.append(updated_content)
                        updated_input_contents_full.append(content)
                for content in outputs:
                    content['substatus'] = ContentStatus.Missing
                    if content['status'] != content['substatus']:
                        content['status'] = content['substatus']
                        updated_content = {'content_id': content['content_id'],
                                           'status': content['substatus'],
                                           'substatus': content['substatus']}
                        updated_contents.append(updated_content)
                        updated_output_contents_full.append(content)

            for content in outputs:
                if content['status'] != content['substatus']:
                    updated_content = {'content_id': content['content_id'],
                                       'status': content['substatus']}
                    content['status'] = content['substatus']
                    updated_contents.append(updated_content)
                    updated_output_contents_full.append(content)
        return updated_contents, updated_input_contents_full, updated_output_contents_full

    def trigger_release_inputs(self, updated_output_contents, work, input_output_maps, final=False):
        to_release_inputs = {}
        for map_id in input_output_maps:
            outputs = input_output_maps[map_id]['outputs'] if 'outputs' in input_output_maps[map_id] else []
            for content in outputs:
                if (content['status'] in [ContentStatus.Available, ContentStatus.FakeAvailable, ContentStatus.FinalFailed, ContentStatus.Missing]
                    or content['substatus'] in [ContentStatus.Available, ContentStatus.FakeAvailable, ContentStatus.FinalFailed, ContentStatus.Missing]):  # noqa W503
                    if content['coll_id'] not in to_release_inputs:
                        to_release_inputs[content['coll_id']] = []
                    to_release_inputs[content['coll_id']].append(content)

        # updated_contents = core_transforms.release_inputs(to_release_inputs)
        updated_contents = core_transforms.release_inputs_by_collection(to_release_inputs, final=final)
        # self.logger.debug("trigger_release_inputs, to_release_inputs: %s" % str(to_release_inputs))
        self.logger.debug("trigger_release_inputs, updated_contents[:10]: %s" % str(updated_contents[:10]))
        return updated_contents

    def poll_inputs_dependency(self, transform, registered_input_output_maps):
        unfinished_inputs = {}
        for map_id in registered_input_output_maps:
            inputs_dependency = registered_input_output_maps[map_id]['inputs_dependency'] if 'inputs_dependency' in registered_input_output_maps[map_id] else []
            for content in inputs_dependency:
                if (content['status'] not in [ContentStatus.Available, ContentStatus.FakeAvailable, ContentStatus.FinalFailed, ContentStatus.Missing]
                    and content['substatus'] not in [ContentStatus.Available, ContentStatus.FakeAvailable, ContentStatus.FinalFailed, ContentStatus.Missing]):  # noqa W503
                    if content['coll_id'] not in unfinished_inputs:
                        unfinished_inputs[content['coll_id']] = []
                    unfinished_inputs[content['coll_id']].append(content)

        # updated_contents = core_transforms.release_inputs(to_release_inputs)
        updated_contents = core_transforms.poll_inputs_dependency_by_collection(unfinished_inputs)
        self.logger.debug("poll_inputs_dependency, updated_contents[:10]: %s" % str(updated_contents[:10]))
        return updated_contents

    def handle_new_transform_real(self, transform):
        """
        Process new transform
        """
        self.logger.info("handle_new_transform: transform_id: %s" % transform['transform_id'])

        work = transform['transform_metadata']['work']
        work.set_work_id(transform['transform_id'])
        work.set_agent_attributes(self.agent_attributes, transform)

        work_name_to_coll_map = core_transforms.get_work_name_to_coll_map(request_id=transform['request_id'])
        work.set_work_name_to_coll_map(work_name_to_coll_map)

        # check contents
        new_input_output_maps = work.get_new_input_output_maps(mapped_input_output_maps={})

        new_input_contents, new_output_contents, new_log_contents, new_input_dependency_contents = self.get_new_contents(transform, new_input_output_maps)
        new_contents = []
        if new_input_contents:
            new_contents = new_contents + new_input_contents
        if new_output_contents:
            new_contents = new_contents + new_output_contents
        if new_log_contents:
            new_contents = new_contents + new_log_contents
        if new_input_dependency_contents:
            new_contents = new_contents + new_input_dependency_contents

        # create processing
        new_processing_model = None
        processing = work.get_processing(new_input_output_maps, without_creating=False)
        self.logger.debug("work get_processing with creating: %s" % processing)
        if processing and not processing.processing_id:
            new_processing_model = {}
            new_processing_model['transform_id'] = transform['transform_id']
            new_processing_model['request_id'] = transform['request_id']
            new_processing_model['workload_id'] = transform['workload_id']
            new_processing_model['status'] = ProcessingStatus.New
            # new_processing_model['expired_at'] = work.get_expired_at(None)
            new_processing_model['expired_at'] = transform['expired_at']

            # if 'processing_metadata' not in processing:
            #     processing['processing_metadata'] = {}
            # if 'processing_metadata' not in new_processing_model:
            #     new_processing_model['processing_metadata'] = {}
            # new_processing_model['processing_metadata'] = processing.processing_metadata

            proc_work = copy.deepcopy(work)
            proc_work.clean_work()
            processing.work = proc_work
            new_processing_model['processing_metadata'] = {'processing': processing}

        msgs = []
        self.logger.info("generate_message: %s" % transform['transform_id'])
        if new_input_contents:
            msg = self.generate_message(transform, files=new_input_contents, msg_type='file', relation_type='input')
            msgs.append(msg)
        if new_output_contents:
            msg = self.generate_message(transform, files=new_output_contents, msg_type='file', relation_type='output')
            msgs.append(msg)

        transform_parameters = {'status': TransformStatus.Transforming,
                                'locking': TransformLocking.Idle,
                                'workload_id': transform['workload_id'],
                                'next_poll_at': datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_time_period),
                                # 'next_poll_at': datetime.datetime.utcnow(),
                                'transform_metadata': transform['transform_metadata']}

        if new_contents:
            work.has_new_updates()

        ret = {'transform': transform,
               'transform_parameters': transform_parameters,
               'new_contents': new_contents,
               # 'update_contents': updated_contents + to_release_input_contents,
               'messages': msgs,
               'new_processing': new_processing_model
               }
        return ret

    def handle_new_transform(self, transform):
        """
        Process new transform
        """
        try:
            ret = self.handle_new_transform_real(transform)
        except Exception as ex:
            self.logger.error(ex)
            self.logger.error(traceback.format_exc())
            if transform['retries'] > self.retries:
                tf_status = TransformStatus.Failed
            else:
                tf_status = TransformStatus.Transforming

            wait_times = max(4, transform['retries'])

            transform_parameters = {'status': tf_status,
                                    'next_poll_at': datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_time_period * wait_times),
                                    'retries': transform['retries'] + 1,
                                    'locking': TransformLocking.Idle}
            ret = {'transform': transform, 'transform_parameters': transform_parameters}
        return ret

    def update_transform(self, ret):
        new_pr_ids, update_pr_ids = [], []
        try:
            self.logger.info("Main thread finishing processing transform: %s" % ret['transform'])
            if ret:
                retry = True
                retry_num = 0
                while retry:
                    retry = False
                    retry_num += 1
                    try:
                        # self.logger.debug("wen: %s" % str(ret['output_contents']))
                        new_pr_ids, update_pr_ids = core_transforms.add_transform_outputs(transform=ret['transform'],
                                                                                          transform_parameters=ret['transform_parameters'],
                                                                                          input_collections=ret.get('input_collections', None),
                                                                                          output_collections=ret.get('output_collections', None),
                                                                                          log_collections=ret.get('log_collections', None),
                                                                                          new_contents=ret.get('new_contents', None),
                                                                                          update_input_collections=ret.get('update_input_collections', None),
                                                                                          update_output_collections=ret.get('update_output_collections', None),
                                                                                          update_log_collections=ret.get('update_log_collections', None),
                                                                                          update_contents=ret.get('update_contents', None),
                                                                                          messages=ret.get('messages', None),
                                                                                          update_messages=ret.get('update_messages', None),
                                                                                          new_processing=ret.get('new_processing', None),
                                                                                          update_processing=ret.get('update_processing', None),
                                                                                          message_bulk_size=self.message_bulk_size)
                    except exceptions.DatabaseException as ex:
                        if 'ORA-00060' in str(ex):
                            self.logger.warn("(cx_Oracle.DatabaseError) ORA-00060: deadlock detected while waiting for resource")
                            if retry_num < 5:
                                retry = True
                                time.sleep(60 * retry_num * 2)
                            else:
                                raise ex
                        else:
                            raise ex
                            # self.logger.error(ex)
                            # self.logger.error(traceback.format_exc())
        except Exception as ex:
            self.logger.error(ex)
            self.logger.error(traceback.format_exc())
            try:
                transform_parameters = {'status': TransformStatus.Transforming,
                                        'next_poll_at': datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_time_period),
                                        'retries': ret['transform']['retries'] + 1,
                                        'locking': TransformLocking.Idle}
                new_pr_ids, update_pr_ids = core_transforms.add_transform_outputs(transform=ret['transform'],
                                                                                  transform_parameters=transform_parameters)
            except Exception as ex:
                self.logger.error(ex)
                self.logger.error(traceback.format_exc())
        return new_pr_ids, update_pr_ids

    def process_new_transform(self, event):
        self.number_workers += 1
        try:
            if event:
                tf_status = [TransformStatus.New, TransformStatus.Ready, TransformStatus.Extend]
                tf = self.get_transform(transform_id=event.transform_id, status=tf_status, locking=True)
                if tf:
                    ret = self.handle_new_transform(tf)
                    new_pr_ids, update_pr_ids = self.update_transform(ret)
                    for pr_id in new_pr_ids:
                        event = NewProcessingEvent(publisher_id=self.id, processing_id=pr_id)
                        self.event_bus.send(event)
                    for pr_id in update_pr_ids:
                        event = UpdateProcessingEvent(publisher_id=self.id, processing_id=pr_id)
                        self.event_bus.send(event)
        except Exception as ex:
            self.logger.error(ex)
            self.logger.error(traceback.format_exc())
        self.number_workers -= 1

    def get_collection_ids(self, collections):
        coll_ids = []
        for coll in collections:
            coll_ids.append(coll.coll_id)
        return coll_ids

    def get_message_type(self, transform_type, input_type='file'):
        if transform_type in [TransformType.StageIn, TransformType.StageIn.value]:
            if input_type == 'work':
                msg_type_str = MessageTypeStr.StageInWork
                msg_type = MessageType.StageInWork
            elif input_type == 'collection':
                msg_type_str = MessageTypeStr.StageInCollection
                msg_type = MessageType.StageInCollection
            else:
                msg_type_str = MessageTypeStr.StageInFile
                msg_type = MessageType.StageInFile
        elif transform_type in [TransformType.ActiveLearning, TransformType.ActiveLearning.value]:
            if input_type == 'work':
                msg_type_str = MessageTypeStr.ActiveLearningWork
                msg_type = MessageType.ActiveLearningWork
            elif input_type == 'collection':
                msg_type_str = MessageTypeStr.ActiveLearningCollection
                msg_type = MessageType.ActiveLearningCollection
            else:
                msg_type_str = MessageTypeStr.ActiveLearningFile
                msg_type = MessageType.ActiveLearningFile
        elif transform_type in [TransformType.HyperParameterOpt, TransformType.HyperParameterOpt.value]:
            if input_type == 'work':
                msg_type_str = MessageTypeStr.HyperParameterOptWork
                msg_type = MessageType.HyperParameterOptWork
            elif input_type == 'collection':
                msg_type_str = MessageTypeStr.HyperParameterOptCollection
                msg_type = MessageType.HyperParameterOptCollection
            else:
                msg_type_str = MessageTypeStr.HyperParameterOptFile
                msg_type = MessageType.HyperParameterOptFile
        elif transform_type in [TransformType.Processing, TransformType.Processing.value]:
            if input_type == 'work':
                msg_type_str = MessageTypeStr.ProcessingWork
                msg_type = MessageType.ProcessingWork
            elif input_type == 'collection':
                msg_type_str = MessageTypeStr.ProcessingCollection
                msg_type = MessageType.ProcessingCollection
            else:
                msg_type_str = MessageTypeStr.ProcessingFile
                msg_type = MessageType.ProcessingFile
        else:
            if input_type == 'work':
                msg_type_str = MessageTypeStr.UnknownWork
                msg_type = MessageType.UnknownWork
            elif input_type == 'collection':
                msg_type_str = MessageTypeStr.UnknownCollection
                msg_type = MessageType.UnknownCollection
            else:
                msg_type_str = MessageTypeStr.UnknownFile
                msg_type = MessageType.UnknownFile
        return msg_type, msg_type_str.value

    def generate_message(self, transform, work=None, collection=None, files=None, msg_type='file', relation_type='input'):
        if msg_type == 'work':
            if not work:
                return None
        elif msg_type == 'collection':
            if not collection:
                return None
            if not work:
                work = transform['transform_metadata']['work']
        else:
            if not files:
                return None

        request_id = transform['request_id']
        workload_id = transform['workload_id']
        i_msg_type, i_msg_type_str = None, None

        if msg_type == 'work':
            i_msg_type, i_msg_type_str = self.get_message_type(transform['transform_type'], input_type='work')
            msg_content = {'msg_type': i_msg_type_str,
                           'request_id': request_id,
                           'workload_id': workload_id,
                           'relation_type': relation_type,
                           'status': transform['status'].name,
                           'output': work.get_output_data(),
                           'error': work.get_terminated_msg()}
            num_msg_content = 1
        elif msg_type == 'collection':
            # fix for old requests
            coll_name = collection.name
            if coll_name.endswith(".idds.stagein"):
                coll_name = coll_name.replace(".idds.stagein", "")

            i_msg_type, i_msg_type_str = self.get_message_type(transform['transform_type'], input_type='collection')
            msg_content = {'msg_type': i_msg_type_str,
                           'request_id': request_id,
                           'workload_id': workload_id,
                           'relation_type': relation_type,
                           'collections': [{'scope': collection.scope,
                                            'name': coll_name,
                                            'status': collection.status.name}],
                           'output': work.get_output_data(),
                           'error': work.get_terminated_msg()}
            num_msg_content = 1
        else:
            i_msg_type, i_msg_type_str = self.get_message_type(transform['transform_type'], input_type='file')
            files_message = []
            for file in files:
                file_status = file['status'].name
                if file['status'] == ContentStatus.FakeAvailable:
                    file_status = ContentStatus.Available.name
                file_message = {'scope': file['scope'],
                                'name': file['name'],
                                'path': file['path'],
                                'status': file_status}
                files_message.append(file_message)
            msg_content = {'msg_type': i_msg_type_str,
                           'request_id': request_id,
                           'workload_id': workload_id,
                           'relation_type': relation_type,
                           'files': files_message}
            num_msg_content = len(files_message)

        msg = {'msg_type': i_msg_type,
               'status': MessageStatus.New,
               'source': MessageSource.Transformer,
               'destination': MessageDestination.Outside,
               'request_id': request_id,
               'workload_id': workload_id,
               'transform_id': transform['transform_id'],
               'num_contents': num_msg_content,
               'msg_content': msg_content}
        return msg

    def syn_collection_status(self, input_collections, output_collections, log_collections, registered_input_output_maps):
        all_updates_flushed, output_statistics = True, {}

        input_status, output_status, log_status = {}, {}, {}
        for map_id in registered_input_output_maps:
            inputs = registered_input_output_maps[map_id]['inputs'] if 'inputs' in registered_input_output_maps[map_id] else []
            outputs = registered_input_output_maps[map_id]['outputs'] if 'outputs' in registered_input_output_maps[map_id] else []
            logs = registered_input_output_maps[map_id]['logs'] if 'logs' in registered_input_output_maps[map_id] else []

            for content in inputs:
                if content['coll_id'] not in input_status:
                    input_status[content['coll_id']] = {'total_files': 0, 'processed_files': 0, 'processing_files': 0, 'bytes': 0}
                input_status[content['coll_id']]['total_files'] += 1

                if content['status'] in [ContentStatus.Available, ContentStatus.Mapped,
                                         ContentStatus.Available.value, ContentStatus.Mapped.value,
                                         ContentStatus.FakeAvailable, ContentStatus.FakeAvailable.value]:
                    input_status[content['coll_id']]['processed_files'] += 1
                    input_status[content['coll_id']]['bytes'] += content['bytes']
                else:
                    input_status[content['coll_id']]['processing_files'] += 1

            for content in outputs:
                if content['coll_id'] not in output_status:
                    output_status[content['coll_id']] = {'total_files': 0, 'processed_files': 0, 'processing_files': 0, 'bytes': 0}
                output_status[content['coll_id']]['total_files'] += 1
                if content['status'] in [ContentStatus.Available, ContentStatus.Available.value,
                                         ContentStatus.FakeAvailable, ContentStatus.FakeAvailable.value]:
                    output_status[content['coll_id']]['processed_files'] += 1
                    output_status[content['coll_id']]['bytes'] += content['bytes']
                else:
                    output_status[content['coll_id']]['processing_files'] += 1

                if content['status'].name not in output_statistics:
                    output_statistics[content['status'].name] = 0
                output_statistics[content['status'].name] += 1

                if content['status'] != content['substatus']:
                    all_updates_flushed = False

            for content in logs:
                if content['coll_id'] not in log_status:
                    log_status[content['coll_id']] = {'total_files': 0, 'processed_files': 0, 'processing_files': 0, 'bytes': 0}
                log_status[content['coll_id']]['total_files'] += 1
                if content['status'] in [ContentStatus.Available, ContentStatus.Available.value,
                                         ContentStatus.FakeAvailable, ContentStatus.FakeAvailable.value]:
                    log_status[content['coll_id']]['processed_files'] += 1
                    log_status[content['coll_id']]['bytes'] += content['bytes']
                else:
                    log_status[content['coll_id']]['processing_files'] += 1

        for coll in input_collections:
            if coll.coll_id in input_status:
                coll.collection['total_files'] = input_status[coll.coll_id]['total_files']
                coll.collection['processed_files'] = input_status[coll.coll_id]['processed_files']
                coll.collection['processing_files'] = input_status[coll.coll_id]['processing_files']

        for coll in output_collections:
            if coll.coll_id in output_status:
                coll.collection['total_files'] = output_status[coll.coll_id]['total_files']
                coll.collection['processed_files'] = output_status[coll.coll_id]['processed_files']
                coll.collection['processing_files'] = output_status[coll.coll_id]['processing_files']
                coll.collection['bytes'] = output_status[coll.coll_id]['bytes']

        for coll in log_collections:
            if coll.coll_id in log_status:
                coll.collection['total_files'] = log_status[coll.coll_id]['total_files']
                coll.collection['processed_files'] = log_status[coll.coll_id]['processed_files']
                coll.collection['processing_files'] = log_status[coll.coll_id]['processing_files']
                coll.collection['bytes'] = log_status[coll.coll_id]['bytes']

        return all_updates_flushed, output_statistics

    def reactive_contents(self, input_output_maps):
        updated_contents = []
        for map_id in input_output_maps:
            inputs = input_output_maps[map_id]['inputs'] if 'inputs' in input_output_maps[map_id] else []
            outputs = input_output_maps[map_id]['outputs'] if 'outputs' in input_output_maps[map_id] else []
            inputs_dependency = input_output_maps[map_id]['inputs_dependency'] if 'inputs_dependency' in input_output_maps[map_id] else []

            all_outputs_available = True
            for content in outputs:
                if not content['status'] in [ContentStatus.Available]:
                    all_outputs_available = False
                    break

            if not all_outputs_available:
                for content in inputs + outputs:
                    update_content = {'content_id': content['content_id'],
                                      'status': ContentStatus.New,
                                      'substatus': ContentStatus.New}
                    updated_contents.append(update_content)
                for content in inputs_dependency:
                    if content['status'] not in [ContentStatus.Available]:
                        update_content = {'content_id': content['content_id'],
                                          'status': ContentStatus.New,
                                          'substatus': ContentStatus.New}
                        updated_contents.append(update_content)
        return updated_contents

    def handle_update_transform_real(self, transform):
        """
        process running transforms
        """
        self.logger.info("handle_update_transform: transform_id: %s" % transform['transform_id'])

        msgs, update_msgs = [], []

        # transform_substatus = None
        t_processing_status = None
        is_operation = False
        if transform['status'] in [TransformStatus.ToCancel, TransformStatus.ToSuspend,
                                   TransformStatus.ToResume, TransformStatus.ToExpire,
                                   TransformStatus.ToFinish, TransformStatus.ToForceFinish]:
            is_operation = True
            if transform['status'] == TransformStatus.ToCancel:
                t_processing_status = ProcessingStatus.ToCancel
                # transform_substatus = TransformStatus.Cancelling
            if transform['status'] == TransformStatus.ToSuspend:
                t_processing_status = ProcessingStatus.ToSuspend
                # transform_substatus = TransformStatus.Suspending
            if transform['status'] == TransformStatus.ToResume:
                t_processing_status = ProcessingStatus.ToResume
                # transform_substatus = TransformStatus.Resuming
            if transform['status'] == TransformStatus.ToExpire:
                t_processing_status = ProcessingStatus.ToExpire
                # transform_substatus = TransformStatus.Expiring
            if transform['status'] == TransformStatus.ToFinish:
                t_processing_status = ProcessingStatus.ToFinish
                # transform_substatus = TransformStatus.Transforming
            if transform['status'] == TransformStatus.ToForceFinish:
                t_processing_status = ProcessingStatus.ToForceFinish
                # transform_substatus = TransformStatus.Transforming

        work = transform['transform_metadata']['work']
        work.set_work_id(transform['transform_id'])
        work.set_agent_attributes(self.agent_attributes, transform)

        # link collections
        input_collections = work.get_input_collections()
        output_collections = work.get_output_collections()
        log_collections = work.get_log_collections()

        for coll in input_collections + output_collections + log_collections:
            coll_model = core_catalog.get_collection(coll_id=coll.coll_id)
            coll.collection = coll_model

        input_coll_ids = self.get_collection_ids(input_collections)
        output_coll_ids = self.get_collection_ids(output_collections)
        log_coll_ids = self.get_collection_ids(log_collections)

        registered_input_output_maps = core_transforms.get_transform_input_output_maps(transform['transform_id'],
                                                                                       input_coll_ids=input_coll_ids,
                                                                                       output_coll_ids=output_coll_ids,
                                                                                       log_coll_ids=log_coll_ids)

        work_name_to_coll_map = core_transforms.get_work_name_to_coll_map(request_id=transform['request_id'])
        work.set_work_name_to_coll_map(work_name_to_coll_map)

        # link processings
        new_processing_model, processing_model, update_processing_model = None, None, {}

        processing = work.get_processing(input_output_maps=[], without_creating=True)
        self.logger.debug("work get_processing: %s" % processing)
        if processing and processing.processing_id:
            processing_model = core_processings.get_processing(processing_id=processing.processing_id)
            work.sync_processing(processing, processing_model)
            processing_metadata = processing_model['processing_metadata']
            if 'errors' in processing_metadata:
                work.set_terminated_msg(processing_metadata['errors'])
            # work.set_processing_output_metadata(processing, processing_model['output_metadata'])
            work.set_output_data(processing.output_data)
            transform['workload_id'] = processing_model['workload_id']

        # check contents
        new_input_output_maps = work.get_new_input_output_maps(registered_input_output_maps)

        new_input_contents, new_output_contents, new_log_contents, new_input_dependency_contents = self.get_new_contents(transform, new_input_output_maps)
        new_contents = []
        if new_input_contents:
            new_contents = new_contents + new_input_contents
        if new_output_contents:
            new_contents = new_contents + new_output_contents
        if new_log_contents:
            new_contents = new_contents + new_log_contents
        if new_input_dependency_contents:
            new_contents = new_contents + new_input_dependency_contents

        # create processing
        if not processing:
            processing = work.get_processing(new_input_output_maps, without_creating=False)
            self.logger.debug("work get_processing with creating: %s" % processing)
        if processing and not processing.processing_id:
            new_processing_model = {}
            new_processing_model['transform_id'] = transform['transform_id']
            new_processing_model['request_id'] = transform['request_id']
            new_processing_model['workload_id'] = transform['workload_id']
            new_processing_model['status'] = ProcessingStatus.New
            # new_processing_model['expired_at'] = work.get_expired_at(None)
            new_processing_model['expired_at'] = transform['expired_at']

            # if 'processing_metadata' not in processing:
            #     processing['processing_metadata'] = {}
            # if 'processing_metadata' not in new_processing_model:
            #     new_processing_model['processing_metadata'] = {}
            # new_processing_model['processing_metadata'] = processing.processing_metadata

            proc_work = copy.deepcopy(work)
            proc_work.clean_work()
            processing.work = proc_work
            new_processing_model['processing_metadata'] = {'processing': processing}
            if t_processing_status is not None:
                new_processing_model['status'] = t_processing_status
                # new_processing_model['substatus'] = t_processing_status

        # check updated contents
        updated_contents, updated_input_contents_full, updated_output_contents_full = [], [], []
        to_release_input_contents = []
        if work.should_release_inputs(processing, self.poll_operation_time_period):
            self.logger.info("get_updated_contents for transform %s" % transform['transform_id'])
            updated_contents, updated_input_contents_full, updated_output_contents_full = self.get_updated_contents(transform, registered_input_output_maps)
            # if work.use_dependency_to_release_jobs() and (updated_output_contents_full or work.has_to_release_inputs()):
            if work.use_dependency_to_release_jobs():
                pass
                self.logger.info("trigger_release_inputs: %s" % transform['transform_id'])
                to_release_input_contents = self.trigger_release_inputs(updated_output_contents_full, work, registered_input_output_maps)
                if not to_release_input_contents:
                    to_release_input_contents = self.poll_inputs_dependency(transform, registered_input_output_maps)

        self.logger.info("generate_message: %s" % transform['transform_id'])
        if new_input_contents:
            msg = self.generate_message(transform, files=new_input_contents, msg_type='file', relation_type='input')
            msgs.append(msg)
        if new_output_contents:
            msg = self.generate_message(transform, files=new_output_contents, msg_type='file', relation_type='output')
            msgs.append(msg)
        if updated_input_contents_full:
            msg = self.generate_message(transform, files=updated_input_contents_full, msg_type='file', relation_type='input')
            msgs.append(msg)
        if updated_output_contents_full:
            msg = self.generate_message(transform, files=updated_output_contents_full, msg_type='file', relation_type='output')
            msgs.append(msg)

        # transform['locking'] = TransformLocking.Idle
        # status_statistics = work.get_status_statistics(registered_input_output_maps)
        self.logger.info("syn_collection_status: %s" % transform['transform_id'])
        all_updates_flushed, output_statistics = self.syn_collection_status(input_collections, output_collections, log_collections, registered_input_output_maps)

        self.logger.info("syn_work_status: %s, transform status: %s" % (transform['transform_id'], transform['status']))
        work.syn_work_status(registered_input_output_maps, all_updates_flushed, output_statistics, to_release_input_contents)
        if work.is_terminated():
            self.logger.info("Transform(%s) work is terminated, trigger to release all final status files" % (transform['transform_id']))
            if work.use_dependency_to_release_jobs():
                pass
                self.logger.info("trigger_release_inputs: %s" % transform['transform_id'])
                to_release_input_contents1 = self.trigger_release_inputs(updated_output_contents_full, work, registered_input_output_maps, final=True)
                to_release_input_contents = to_release_input_contents + to_release_input_contents1

        to_resume_transform = False
        reactivated_contents = []
        if transform['status'] in [TransformStatus.ToCancel]:
            transform['status'] = TransformStatus.Cancelling
            work.tocancel = True
        elif transform['status'] in [TransformStatus.ToSuspend]:
            transform['status'] = TransformStatus.Suspending
            work.tosuspend = True
        elif transform['status'] in [TransformStatus.ToResume]:
            transform['status'] = TransformStatus.Resuming
            transform['retries'] = 0
            work.toresume = True
            to_resume_transform = True
            reactivated_contents = self.reactive_contents(registered_input_output_maps)
            # reactive collections
            for coll in input_collections:
                coll.status = CollectionStatus.Open
            for coll in output_collections:
                coll.status = CollectionStatus.Open
            for coll in log_collections:
                coll.status = CollectionStatus.Open
        elif transform['status'] in [TransformStatus.ToExpire]:
            transform['status'] = TransformStatus.Expiring
            work.toexpire = True
        elif transform['status'] in [TransformStatus.ToFinish]:
            transform['status'] = TransformStatus.Transforming
            work.tofinish = True
        elif transform['status'] in [TransformStatus.ToForceFinish]:
            transform['status'] = TransformStatus.Transforming
            work.toforcefinish = True
        elif work.is_finished():
            transform['status'] = TransformStatus.Finished
            msg = self.generate_message(transform, work=work, msg_type='work')
            msgs.append(msg)
            for coll in input_collections:
                coll.status = CollectionStatus.Closed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='input')
                msgs.append(msg)
            for coll in output_collections:
                coll.status = CollectionStatus.Closed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='output')
                msgs.append(msg)
            for coll in log_collections:
                coll.status = CollectionStatus.Closed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='log')
                msgs.append(msg)
        elif work.is_subfinished():
            transform['status'] = TransformStatus.SubFinished
            msg = self.generate_message(transform, work=work, msg_type='work')
            msgs.append(msg)
            for coll in input_collections:
                coll.status = CollectionStatus.SubClosed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='input')
                msgs.append(msg)
            for coll in output_collections:
                coll.status = CollectionStatus.SubClosed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='output')
                msgs.append(msg)
            for coll in log_collections:
                coll.status = CollectionStatus.SubClosed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='log')
                msgs.append(msg)
        elif work.is_failed():
            transform['status'] = TransformStatus.Failed
            msg = self.generate_message(transform, work=work, msg_type='work')
            msgs.append(msg)
            for coll in input_collections:
                coll.status = CollectionStatus.Failed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='input')
                msgs.append(msg)
            for coll in output_collections:
                coll.status = CollectionStatus.Failed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='output')
                msgs.append(msg)
            for coll in log_collections:
                coll.status = CollectionStatus.Failed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='log')
                msgs.append(msg)
        elif work.is_expired():
            transform['status'] = TransformStatus.Expired
            msg = self.generate_message(transform, work=work, msg_type='work')
            msgs.append(msg)
            for coll in input_collections:
                coll.status = CollectionStatus.SubClosed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='input')
                msgs.append(msg)
            for coll in output_collections:
                coll.status = CollectionStatus.SubClosed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='output')
                msgs.append(msg)
            for coll in log_collections:
                coll.status = CollectionStatus.SubClosed
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='log')
                msgs.append(msg)
        elif work.is_cancelled():
            transform['status'] = TransformStatus.Cancelled
            msg = self.generate_message(transform, work=work, msg_type='work')
            msgs.append(msg)
            for coll in input_collections:
                coll.status = CollectionStatus.Cancelled
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='input')
                msgs.append(msg)
            for coll in output_collections:
                coll.status = CollectionStatus.Cancelled
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='output')
                msgs.append(msg)
            for coll in log_collections:
                coll.status = CollectionStatus.Cancelled
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='log')
                msgs.append(msg)
        elif work.is_suspended():
            transform['status'] = TransformStatus.Suspended
            msg = self.generate_message(transform, work=work, msg_type='work')
            msgs.append(msg)
            for coll in input_collections:
                coll.status = CollectionStatus.Suspended
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='input')
                msgs.append(msg)
            for coll in output_collections:
                coll.status = CollectionStatus.Suspended
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='output')
                msgs.append(msg)
            for coll in log_collections:
                coll.status = CollectionStatus.Suspended
                msg = self.generate_message(transform, work=work, collection=coll, msg_type='collection', relation_type='log')
                msgs.append(msg)
        else:
            transform['status'] = TransformStatus.Transforming

        if not is_operation:
            next_poll_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_time_period)
        else:
            if to_resume_transform:
                next_poll_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_operation_time_period * 5)
            else:
                next_poll_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_operation_time_period)

        # reset retries to 0 when it succeed
        transform['retries'] = 0

        transform_parameters = {'status': transform['status'],
                                'locking': TransformLocking.Idle,
                                'workload_id': transform['workload_id'],
                                'next_poll_at': next_poll_at,
                                'retries': transform['retries'],
                                'transform_metadata': transform['transform_metadata']}
        # if transform_substatus:
        #     transform_parameters['substatus'] = transform_substatus

        if new_contents or updated_contents or to_release_input_contents:
            work.has_new_updates()

        # print(input_collections)
        ret = {'transform': transform,
               'transform_parameters': transform_parameters,
               # 'update_input_collections': copy.deepcopy(input_collections) if input_collections else input_collections,
               # 'update_output_collections': copy.deepcopy(output_collections) if output_collections else output_collections,
               # 'update_log_collections': copy.deepcopy(log_collections) if log_collections else log_collections,
               'update_input_collections': input_collections,
               'update_output_collections': output_collections,
               'update_log_collections': log_collections,
               'new_contents': new_contents,
               'update_contents': updated_contents + to_release_input_contents + reactivated_contents,
               'messages': msgs,
               'update_messages': update_msgs,
               'new_processing': new_processing_model,
               'update_processing': update_processing_model}
        return ret

    def process_running_transform_message(self, transform, messages):
        """
        process running transform message
        """
        try:
            self.logger.info("process_running_transform_message: transform_id: %s, messages: %s" % (transform['transform_id'], str(messages) if messages else messages))
            msg = messages[0]
            message = messages[0]['msg_content']
            if message['command'] == 'update_transform':
                parameters = message['parameters']
                parameters['locking'] = TransformLocking.Idle
                ret = {'transform': transform,
                       'transform_parameters': parameters,
                       'update_messages': [{'msg_id': msg['msg_id'], 'status': MessageStatus.Delivered}]
                       }
            else:
                self.logger.error("Unknown message: %s" % str(msg))
                ret = {'transform': transform,
                       'transform_parameters': {'locking': TransformLocking.Idle},
                       'update_messages': [{'msg_id': msg['msg_id'], 'status': MessageStatus.Failed}]
                       }
        except Exception as ex:
            self.logger.error(ex)
            self.logger.error(traceback.format_exc())
            if transform['retries'] > self.retries:
                tf_status = TransformStatus.Failed
            else:
                tf_status = TransformStatus.Transforming

            wait_times = max(4, transform['retries'])

            ret = {'transform': transform,
                   'transform_parameters': {'status': tf_status,
                                            'next_poll_at': datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_time_period * wait_times),
                                            'locking': TransformLocking.Idle,
                                            'retries': transform['retries'] + 1,
                                            'errors': {'msg': '%s: %s' % (ex, traceback.format_exc())}}}
        return ret

    def handle_update_transform(self, transform):
        """
        Process running transform
        """
        try:
            msgs = self.get_transform_message(transform_id=transform['transform_id'], bulk_size=1)
            if msgs:
                self.logger.info("Main thread processing running transform with message: %s" % transform)
                ret = self.process_running_transform_message(transform, msgs)
            else:
                self.logger.info("Main thread processing running transform: %s" % transform)
                ret = self.process_running_transform_real(transform)
        except Exception as ex:
            self.logger.error(ex)
            self.logger.error(traceback.format_exc())
            if transform['retries'] > self.retries:
                tf_status = TransformStatus.Failed
            else:
                tf_status = TransformStatus.Transforming

            wait_times = max(4, transform['retries'])

            transform_parameters = {'status': tf_status,
                                    'next_poll_at': datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_time_period * wait_times),
                                    'retries': transform['retries'] + 1,
                                    'locking': TransformLocking.Idle}
            ret = {'transform': transform, 'transform_parameters': transform_parameters}
        return ret

    def process_update_transform(self, event):
        self.number_workers += 1
        try:
            if event:
                tf_status = [TransformStatus.Transforming,
                             TransformStatus.ToCancel, TransformStatus.Cancelling,
                             TransformStatus.ToSuspend, TransformStatus.Suspending,
                             TransformStatus.ToExpire, TransformStatus.Expiring,
                             TransformStatus.ToResume, TransformStatus.Resuming,
                             TransformStatus.ToFinish, TransformStatus.ToForceFinish]
                tf = self.get_transform(transform_id=event.transform_id, status=tf_status, locking=True)
                if tf:
                    ret = self.handle_update_transform(tf)
                    event = UpdateRequestEvent(publisher_id=self.id, request_id=tf['request_id'])
                    self.event_bus.send(event)
                    new_pr_ids, update_pr_ids = self.update_transform(ret)
                    for pr_id in new_pr_ids:
                        event = NewProcessingEvent(publisher_id=self.id, processing_id=pr_id)
                        self.event_bus.send(event)
                    for pr_id in update_pr_ids:
                        event = UpdateProcessingEvent(publisher_id=self.id, processing_id=pr_id)
                        self.event_bus.send(event)
        except Exception as ex:
            self.logger.error(ex)
            self.logger.error(traceback.format_exc())
        self.number_workers -= 1

    def handle_abort_transform(self, transform):
        """
        process abort transform
        """
        try:
            work = transform['transform_metadata']['work']
            work.to_cancel = True
            tf_status = TransformStatus.Cancelling
            next_poll_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_operation_time_period)

            transform_parameters = {'status': tf_status,
                                    'locking': TransformLocking.Idle,
                                    'next_poll_at': next_poll_at}
            ret = {'transform': transform, 'transform_parameters': transform_parameters}
            return ret
        except Exception as ex:
            self.logger.error(ex)
            self.logger.error(traceback.format_exc())
        return None

    def process_abort_transform(self, event):
        self.number_workers += 1
        try:
            if event:
                tf = self.get_transform(transform_id=event.transform_id, locking=True)
                if tf['status'] in [TransformStatus.Finished, TransformStatus.SubFinished,
                                    TransformStatus.Failed, TransformStatus.Cancelled,
                                    TransformStatus.Suspended, TransformStatus.Expired]:
                    ret = {'transform': tf,
                           'transform_parameters': {'locking': TransformLocking.Idle,
                                                    'errors': {'extra_msg': "Transform is already terminated. Cannot be aborted"}}}
                    if 'msg' in tf['errors']:
                        ret['parameters']['errors']['msg'] = tf['errors']['msg']
                    self.update_transform(ret)
                else:
                    ret = self.handle_abort_transform(tf)
                    if ret:
                        self.update_transform(ret)

                    work = tf['transform_metadata']['work']
                    work.set_work_id(tf['transform_id'])
                    work.set_agent_attributes(self.agent_attributes, tf)

                    processing = work.get_processing(input_output_maps=[], without_creating=True)
                    if processing and processing.processing_id:
                        event = AbortProcessingEvent(publisher_id=self.id, processing_id=processing.processing_id)
                        self.event_bus.send(event)
                    else:
                        event = UpdateTransformEvent(publisher_id=self.id, transform_id=tf['transform_id'])
                        self.event_bus.send(event)
        except Exception as ex:
            self.logger.error(ex)
            self.logger.error(traceback.format_exc())
        self.number_workers -= 1

    def handle_resume_transform(self, transform):
        """
        process resume transform
        """
        try:
            work = transform['transform_metadata']['work']
            work.set_work_id(transform['transform_id'])
            work.set_agent_attributes(self.agent_attributes, transform)

            # link collections
            input_collections = work.get_input_collections()
            output_collections = work.get_output_collections()
            log_collections = work.get_log_collections()

            for coll in input_collections + output_collections + log_collections:
                coll_model = core_catalog.get_collection(coll_id=coll.coll_id)
                coll.collection = coll_model

            input_coll_ids = self.get_collection_ids(input_collections)
            output_coll_ids = self.get_collection_ids(output_collections)
            log_coll_ids = self.get_collection_ids(log_collections)

            registered_input_output_maps = core_transforms.get_transform_input_output_maps(transform['transform_id'],
                                                                                           input_coll_ids=input_coll_ids,
                                                                                           output_coll_ids=output_coll_ids,
                                                                                           log_coll_ids=log_coll_ids)

            work.toresume = True
            reactivated_contents = self.reactive_contents(registered_input_output_maps)
            # reactive collections
            for coll in input_collections:
                coll.status = CollectionStatus.Open
            for coll in output_collections:
                coll.status = CollectionStatus.Open
            for coll in log_collections:
                coll.status = CollectionStatus.Open

            tf_status = TransformStatus.Resuming
            next_poll_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=self.poll_operation_time_period)

            transform_parameters = {'status': tf_status,
                                    'retries': 0,
                                    'locking': TransformLocking.Idle,
                                    'next_poll_at': next_poll_at}
            ret = {'transform': transform,
                   'transform_parameters': transform_parameters,
                   'update_input_collections': input_collections,
                   'update_output_collections': output_collections,
                   'update_log_collections': log_collections,
                   'update_contents': reactivated_contents}
            return ret
        except Exception as ex:
            self.logger.error(ex)
            self.logger.error(traceback.format_exc())
        return None

    def process_resume_transform(self, event):
        self.number_workers += 1
        try:
            if event:
                tf = self.get_transform(transform_id=event.transform_id, locking=True)
                if tf['status'] in [TransformStatus.Finished]:
                    ret = {'transform': tf,
                           'transform_parameters': {'locking': TransformLocking.Idle,
                                                    'errors': {'extra_msg': "Transform is already finished. Cannot be resumed"}}}
                    if 'msg' in tf['errors']:
                        ret['parameters']['errors']['msg'] = tf['errors']['msg']
                    self.update_transform(ret)
                else:
                    ret = self.handle_abort_transform(tf)
                    if ret:
                        self.update_transform(ret)

                    work = tf['transform_metadata']['work']
                    work.set_work_id(tf['transform_id'])
                    work.set_agent_attributes(self.agent_attributes, tf)

                    processing = work.get_processing(input_output_maps=[], without_creating=True)
                    if processing and processing.processing_id:
                        event = ResumeProcessingEvent(publisher_id=self.id, processing_id=processing.processing_id)
                        self.event_bus.send(event)
                    else:
                        event = UpdateTransformEvent(publisher_id=self.id, transform_id=tf['transform_id'])
                        self.event_bus.send(event)
        except Exception as ex:
            self.logger.error(ex)
            self.logger.error(traceback.format_exc())
        self.number_workers -= 1

    def clean_locks(self):
        self.logger.info("clean locking")
        core_transforms.clean_locking()

    def init_event_function_map(self):
        self.event_func_map = {
            NewTransformEvent._event_type: {
                'pre_check': self.is_ok_to_run_more_requests,
                'exec_func': self.process_new_request
            },
            UpdateTransformEvent._event_type: {
                'pre_check': self.is_ok_to_run_more_requests,
                'exec_func': self.process_update_request
            },
            AbortTransformEvent._event_type: {
                'pre_check': self.is_ok_to_run_more_requests,
                'exec_func': self.process_abort_request
            },
            ResumeTransformEvent._event_type: {
                'pre_check': self.is_ok_to_run_more_requests,
                'exec_func': self.process_resume_request
            }
        }

    def run(self):
        """
        Main run function.
        """
        try:
            self.logger.info("Starting main thread")

            self.load_plugins()

            self.add_default_tasks()

            self.init_event_function_map()

            task = self.create_task(task_func=self.get_new_transforms, task_output_queue=None, task_args=tuple(), task_kwargs={}, delay_time=60, priority=1)
            self.add_task(task)
            task = self.create_task(task_func=self.get_running_transforms, task_output_queue=None, task_args=tuple(), task_kwargs={}, delay_time=60, priority=1)
            self.add_task(task)
            task = self.create_task(task_func=self.clean_locks, task_output_queue=None, task_args=tuple(), task_kwargs={}, delay_time=1800, priority=1)
            self.add_task(task)

            self.execute()
        except KeyboardInterrupt:
            self.stop()


if __name__ == '__main__':
    agent = Transformer()
    agent()
