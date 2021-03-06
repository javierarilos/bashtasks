#!/usr/bin/env python
""" executor is the module that executes bashtasks
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
import logging
import pika

from bashtasks.rabbit_util import connect_and_declare, close_channel_and_conn
from bashtasks.constants import DestinationNames, TASK_REQUESTS_POOL, TASK_RESPONSES_POOL
from bashtasks.logger import get_logger

channels = []  # stores all executor thread channels.
stop = False  # False until the executor is asked to stop
MB_10 = 10485760

DEFAULT_DESTINATION = DestinationNames.get_for(TASK_REQUESTS_POOL)


def curr_module_name():
    return os.path.splitext(os.path.basename(__file__))[0]


def currtimemillis():
    return int(round(time.time() * 1000))


def get_executor_name():
    return ":".join((gethostname(), str(os.getpid()), threading.current_thread().name))


def get_thread_name(worker):
    return 'worker_th_' + str(worker)


def start_executors(workers=1, host='127.0.0.1', port=5672, usr='guest', pas='guest',
                    queue=DEFAULT_DESTINATION, tasks_nr=1, max_retries=0, verbose=False,
                    custom_callback=None, ok_returncodes=(0, )):
    worker_ths = []
    for worker in range(0, workers):
        worker_th = threading.Thread(target=start_executor,
                                     kwargs=({'host': host, 'port': port, 'usr': usr, 'pas': pas,
                                              'queue': queue, 'tasks_nr': tasks_nr,
                                              'max_retries': max_retries, 'verbose': verbose,
                                              'custom_callback': custom_callback,
                                              'ok_returncodes': ok_returncodes}),
                                     name=get_thread_name(worker))
        worker_th.daemon = True

        worker_th.start()
        worker_ths.append(worker_th)

    while not stop:
        sleep(1)


def start_executor(host='127.0.0.1', port=5672, usr='guest', pas='guest', queue=DEFAULT_DESTINATION,
                   tasks_nr=1, max_retries=0, verbose=False, custom_callback=None,
                   ok_returncodes=(0, )):
    curr_th_name = threading.current_thread().name
    logger = get_logger(name=curr_module_name())
    logger.info(">> Starting executor %s connecting to rabbitmq: %s:%s@%s for executing %d tasks.",
                curr_th_name, usr, pas, host, tasks_nr)

    ch = connect_and_declare(host=host, port=port, usr=usr, pas=pas, destinations=queue)
    ch.basic_qos(prefetch_count=1)  # consume msgs one at a time

    channels.append(ch)

    def is_ok_returncode(code):
        return code in ok_returncodes

    def create_response_for(msg):
        resp = {}
        resp.update(msg)
        resp['executor_name'] = get_executor_name()
        resp['retries'] = msg.get('retries', 0)
        resp['max_retries'] = msg.get('max_retries', 0)
        return resp

    def should_retry(response_msg):
        is_error = response_msg['returncode'] != 0
        returncode = response_msg['returncode']
        is_retriable = returncode not in response_msg['non_retriable']
        current_retries = response_msg.get('retries', 0)
        msg_max_retries = response_msg.get('max_retries', max_retries)
        retries_pending = current_retries < msg_max_retries
        return is_error and is_retriable and retries_pending

    def send_response(response_msg):
        if should_retry(response_msg):
            logger.debug('---- retrying msg correlation_id: %d current_retries: %d of %d',
                         response_msg['correlation_id'], response_msg['retries'],
                         response_msg['max_retries'])
            tgt_exch = TASK_REQUESTS_POOL
            response_msg['retries'] += 1
        else:
            tgt_exch = TASK_RESPONSES_POOL

        response_str = json.dumps(response_msg)
        props = pika.BasicProperties(
                             delivery_mode=2,  # make message persistent
                          )
        ch.basic_publish(exchange=tgt_exch, routing_key='', body=response_str, properties=props)

    def tasks_nr_generator(tasks_nr):
        tasks_nr_gen = tasks_nr
        while True:
            tasks_nr_gen = tasks_nr_gen - 1 if tasks_nr_gen > 0 else tasks_nr_gen
            yield tasks_nr_gen

    def trace_msg(msg, context_info=''):
        logger.info(u'------------- MSG: %s', context_info)
        for key, value in msg.items():
            try:
                logger.info(u'\t%s:-> %s', key, value)
            except Exception as e:
                logger.error('error in trace message::::', exc_info=True)
                sys.exit(777)
        logger.info('---------------------------------------------')

    def execute_command(command):
        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        o, e = p.communicate()

        return p.returncode, o.decode('utf-8'), e.decode('utf-8')

    def handle_message(ch, method, properties, body):
        msg = json.loads(body.decode('utf-8'))
        logger.debug(">>>> msg received: %s from queue %s : correlation_id %d command: %s",
                     curr_th_name, queue, msg['correlation_id'], msg['command'][:50])
        try:
            response_msg = create_response_for(msg)
            response_msg['pre_command_ts'] = currtimemillis()

            returncode, out, err = command_callback(msg['command'])

            response_msg['post_command_ts'] = currtimemillis()
            response_msg['returncode'] = returncode
            response_msg['stdout'] = out
            response_msg['stderr'] = err

        except Exception as exc:
            logger.error('**** Command execution error. Exception for : correlation_id %d ',
                         response_msg['correlation_id'], exc_info=True)
            response_msg['post_command_ts'] = currtimemillis()
            response_msg['returncode'] = -3791
            response_msg['stdout'] = 'Exception trying to execute command.'
            response_msg['stderr'] = repr(exc)
        finally:
            if verbose or not is_ok_returncode(response_msg['returncode']):
                trace_msg(msg, context_info='Request msg: '+str(msg['correlation_id']))
                resp_header = 'Response msg: '+str(response_msg['correlation_id']) + \
                              ' returncode: '+str(response_msg['returncode'])
                trace_msg(response_msg, context_info=resp_header)
            send_response(response_msg)
            ch.basic_ack(method.delivery_tag)

        tasks_nr_new_elem = next(tasks_nr_gen)

        logger.debug("<<<< executed by: executor %s correlation_id: %d pending: %d",
                     curr_th_name, response_msg['correlation_id'], tasks_nr_new_elem)

        if tasks_nr_new_elem == 0:
            logger.info('==== no more tasks to execute. Exiting.')
            stop_and_exit()

    tasks_nr_gen = tasks_nr_generator(tasks_nr)

    command_callback = custom_callback if custom_callback else execute_command

    ch.basic_consume(handle_message, queue=queue, no_ack=False)
    logger.info("<< Ready: executor %s connected to rabbitmq: %s:%s@%s",
                curr_th_name, usr, pas, host)
    ch.start_consuming()


def stop_ampq_channels():
    worker_stopping = 1
    logger = get_logger(name=curr_module_name())
    for ch in channels:
        logger.info('\tStopping worker (%d)...', worker_stopping)

        if ch.is_open:
            try:
                ch.close()
            except Exception as e:
                logger.error('Exception closing channel', exc_info=True)
        logger.info('\tStopped worker (%d).', worker_stopping)
        worker_stopping += 1


def stop_and_exit():
    stop_ampq_channels()
    logger = get_logger(name=curr_module_name())
    logger.info('Stopped all AMQP channels.')
    global stop
    stop = True


def register_signals_handling():
    def signal_handler(signal, frame):
        logger = get_logger(name=curr_module_name())
        logger.info('Received signal: %s', str(signal))
        stop_and_exit()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
