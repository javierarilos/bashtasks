#!/usr/bin/env python
""" executor is the process that executes bashtasks
"""
import signal
import argparse
import subprocess
import sys
import json
import time
import os
import threading
from socket import gethostname
from time import sleep

from bashtasks.rabbit_util import connect_and_declare, close_channel_and_conn
from bashtasks.constants import TASK_REQUESTS_POOL, TASK_RESPONSES_POOL

channels = []  # stores all executor thread channels.
stop = False   # False until the executor is asked to stop


def currtimemillis():
    return int(round(time.time() * 1000))


def get_executor_name():
    return ":".join((gethostname(), str(os.getpid()), threading.current_thread().name))


def get_thread_name():
    return 'worker_th_' + str(x)


def create_response_for(msg):
    return {
        'correlation_id': msg['correlation_id'],
        'reply_to': msg['reply_to'],
        'command': msg['command'],
        'request_ts': msg['request_ts'],
        'executor_name': get_executor_name(),
        'retries': msg.get('retries', 0)
    }


def start_executor(host='127.0.0.1', usr='guest', pas='guest', tasks_nr=1, max_retries=0):
    curr_th_name = threading.current_thread().name
    print(">> Starting executor", curr_th_name, "connecting to rabbitmq:", host, usr, pas,
          "executing", tasks_nr, "tasks.")

    ch = connect_and_declare(host=host, usr=usr, pas=pas)
    ch.basic_qos(prefetch_count=1)  # consume msgs one at a time

    channels.append(ch)

    def should_retry(response_msg):
        is_error = response_msg['returncode'] != 0
        current_retries = response_msg.get('retries', 0)
        retries_pending = current_retries < max_retries
        return is_error and retries_pending

    def send_response(response_msg):
        if should_retry(response_msg):
            print('---- retrying msg correlation_id: {} '
                  'current_retries: {}'
                  .format(response_msg['correlation_id'], response_msg['retries']))
            tgt_exch = TASK_REQUESTS_POOL
            response_msg['retries'] += 1
        else:
            tgt_exch = TASK_RESPONSES_POOL

        response_str = json.dumps(response_msg)
        ch.basic_publish(exchange=tgt_exch, routing_key='', body=response_str)

    def handle_command_request(ch, method, properties, body):
        msg = json.loads(body.decode('utf-8'))
        print(">>>> msg received: ", curr_th_name, "from queue ", TASK_REQUESTS_POOL,
              " : correlation_id", msg['correlation_id'], "command: ", msg['command'])

        try:
            response_msg = create_response_for(msg)
            response_msg['pre_command_ts'] = currtimemillis()

            p = subprocess.Popen(msg['command'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            o, e = p.communicate()

            response_msg['post_command_ts'] = currtimemillis()
            response_msg['returncode'] = p.returncode
            response_msg['stdout'] = o.decode('utf-8')
            response_msg['stderr'] = e.decode('utf-8')

        except Exception as exc:
            print('==== got exception for : correlation_id', response_msg['correlation_id'], exc)
            response_msg['post_command_ts'] = currtimemillis()
            response_msg['returncode'] = -3791
            response_msg['stdout'] = 'Exception trying to execute command.'
            response_msg['stderr'] = repr(exc)

        finally:
            send_response(response_msg)
            ch.basic_ack(method.delivery_tag)

        nonlocal tasks_nr
        tasks_nr = tasks_nr - 1 if tasks_nr > 0 else tasks_nr
        if response_msg['returncode'] != 0:
            print('****************************************** ERR ', response_msg['correlation_id'])
            print('returncode:', response_msg['returncode'])
            print('stdout:', response_msg['stdout'])
            print('stderr:', response_msg['stderr'])
            print('******************************************')

        print("<<<< executed by: executor", curr_th_name, "correlation_id:",
              response_msg['correlation_id'], "pending:", tasks_nr)
        if tasks_nr == 0:
            print('==== no more tasks to execute. Exiting.')
            stop_and_exit()

    ch.basic_consume(handle_command_request, queue=TASK_REQUESTS_POOL, no_ack=False)
    print("<< Ready: executor", curr_th_name, "connected to rabbitmq:", host, usr, pas)
    ch.start_consuming()


def stop_ampq_channels():
    worker_stopping = 1
    for ch in channels:
        print('\tStopping worker ({})...'.format((worker_stopping,)))

        if ch.is_open:
            try:
                ch.close()
            except Exception as e:
                print('Exception closing channel'+str(e))
        print('\tStopped worker ({}).'.format((worker_stopping,)))
        worker_stopping += 1


def stop_and_exit():
    stop_ampq_channels()
    print('Stopped all AMQP channels.')
    global stop
    stop = True


def register_signals_handling():
    def signal_handler(signal, frame):
        print('Received signal: ', str(signal))
        stop_and_exit()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=globals()['__doc__'], add_help=True)
    parser.add_argument('--host', default='127.0.0.1', dest='host')
    parser.add_argument('--port', default=5672, dest='port', type=int)
    parser.add_argument('--user', default='guest', dest='usr')
    parser.add_argument('--pass', default='guest', dest='pas')
    parser.add_argument('--workers', default=1, dest='workers', type=int)
    parser.add_argument('--tasks', default=-1, dest='tasks_nr', type=int)
    parser.add_argument('--max-retries', default=0, dest='max_retries', type=int)

    register_signals_handling()

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    args = parser.parse_args()
    worker_ths = []
    for x in range(0, args.workers):
        worker_th = threading.Thread(target=start_executor,
                                     args=(args.host, args.usr, args.pas,
                                           args.tasks_nr, args.max_retries),
                                     name=get_thread_name(),
                                     daemon=True)
        worker_th.start()
        worker_ths.append(worker_th)

    while not stop:
        sleep(1)

    print('Executor exiting now.')
