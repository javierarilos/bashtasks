#!/usr/bin/env python
""" Execute a task using bashtasks and prints the response to stdout.
"""
import sys
import json
import time
import argparse

import bashtasks as bashtasks_mod

from bashtasks.constants import TASK_REQUESTS_POOL
from bashtasks.constants import DestinationNames
DEFAULT_DESTINATION = DestinationNames.get_for(TASK_REQUESTS_POOL)

parser = argparse.ArgumentParser(description=globals()['__doc__'], add_help=True)
parser.add_argument('--host', default='127.0.0.1', dest='host')
parser.add_argument('--port', default=5672, dest='port', type=int)
parser.add_argument('--user', default='guest', dest='usr')
parser.add_argument('--pass', default='guest', dest='pas')
parser.add_argument('--max-retries', default=None, dest='max_retries', type=int)
parser.add_argument('--no-wait', default=False, action='store_true', dest='fire_and_forget')
parser.add_argument('--command', required=True, dest='command',
                    metavar='"COMMAND" to execute. Better wrapped with quotes (")')
parser.add_argument('--destination', dest='destination', default=DEFAULT_DESTINATION,
                    metavar='"destination" (exchange->queue) to send the message to.')

if len(sys.argv) == 1:
    parser.print_help()
    sys.exit(1)

args = parser.parse_args()
args.command = args.command.split()


def currtimemillis():
    return int(round(time.time() * 1000))

start_ts = currtimemillis()

bashtasks = bashtasks_mod.init(host=args.host, port=args.port, usr=args.usr, pas=args.pas)

if args.fire_and_forget:
    bashtasks.post_task(args.command, max_retries=args.max_retries, destination=args.destination)
    sys.exit(0)

response_msg = bashtasks.execute_task(args.command, max_retries=args.max_retries,
                                      destination=args.destination)

total_time = currtimemillis() - response_msg['request_ts']
command_time = response_msg['post_command_ts'] - response_msg['pre_command_ts']
print('=======================================================================================')
print("received response: returncode : " + str(response_msg['returncode']))
print("                   command    : " + ' '.join(response_msg['command']))
print("                   executor   : " + response_msg['executor_name'])
print("                   retries    : " + str(response_msg['retries']))
if response_msg['returncode'] != 0:
    print("                   stderr     : ")
    print('_______________________________________________________________________________________')
    print(response_msg['stderr'])
    print('_______________________________________________________________________________________')
print("                   stdout     : ")
print('_______________________________________________________________________________________')
print(response_msg['stdout'])
print('_______________________________________________________________________________________')
print("Time:")
print("    total   : " + str(total_time) + "ms.")
print("    command : " + str(command_time) + "ms.")
print('=======================================================================================')

sys.exit(0)
