from datetime import datetime
import time

import bashtasks.rabbit_util as rabbit_util


def assertMessageInQueue(queue_name, channel=None, timeout=3,
                         host='127.0.0.1', usr='guest', port=5672, pas='guest'):
    if not channel:
        channel = rabbit_util.connect(host=host, usr=usr, port=port, pas=pas).channel()

    start_waiting = datetime.now()
    while True:
        method_frame, header_frame, body = channel.basic_get(queue_name)
        if body:
            channel.basic_ack(method_frame.delivery_tag)
            return body
        else:
            if (datetime.now() - start_waiting).total_seconds() > timeout:
                raise Exception('Timeout ({}secs) exceeded waiting for message in queue: "{}"'
                                .format(timeout, queue_name))
            time.sleep(0.01)
