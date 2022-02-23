#!/usr/bin/env python

"""
Test client.
"""

import argparse
import os

os.environ['PANDA_AUTH'] = 'oidc'
os.environ['PANDA_URL_SSL'] = 'https://pandaserver-doma.cern.ch:25443/server/panda'
os.environ['PANDA_URL'] = 'http://pandaserver-doma.cern.ch:25080/server/panda'
os.environ['PANDA_AUTH_VO'] = 'Rubin'
# os.environ['PANDA_CONFIG_ROOT'] = '~/.panda/'

from idds.common.constants import RequestStatus, ProcessingStatus
import idds.common.utils as idds_utils
import pandaclient.idds_api


parser = argparse.ArgumentParser()
parser.add_argument('--workflow_id', dest='workflow_id', action='store', help='Workflow to kill', required=True)
parser.add_argument('--task_id', dest='task_id', action='store', help='Task to kill', required=False)


def kill_workflow_task(idds_server, request_id, task_id=None):
    if task_id is None:
        msg = {'command': 'update_request', 'parameters': {'status': RequestStatus.ToCancel}}
    else:
        msg = {'command': 'update_processing', 'parameters': [{'workload_id': task_id, 'status': ProcessingStatus.ToCancel}]}

    c = pandaclient.idds_api.get_api(idds_utils.json_dumps,
                                     idds_host=idds_server, compress=True, manager=False)
    ret = c.send_message(request_id=request_id, msg=msg)
    print("Command is sent to iDDS: ", str(ret))


if __name__ == '__main__':
    host = "https://aipanda015.cern.ch:443/idds"

    args = parser.parse_args()
    kill_workflow_task(host, args.workflow_id, args.task_id)
