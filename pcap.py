#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# pcap.py - Saves messages from pcap files in Redis.
#
# Copyright (c) Addy Yeow Chin Heng <ayeowch@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""
Saves messages from pcap files in Redis.
"""

import dpkt
import glob
import logging
import os
import redis
import socket
import sys
import time
from collections import defaultdict
from ConfigParser import ConfigParser
from Queue import PriorityQueue

from protocol import (ProtocolError, HeaderTooShortError, PayloadTooShortError,
                      Serializer)

# Redis connection setup
REDIS_SOCKET = os.environ.get('REDIS_SOCKET', "/tmp/redis.sock")
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)
REDIS_CONN = redis.StrictRedis(unix_socket_path=REDIS_SOCKET,
                               password=REDIS_PASSWORD)

SETTINGS = {}


class Stream(object):
    """
    Implements a stream object with generator function to iterate over the
    queued segments while keeping track of captured timestamp.
    """
    def __init__(self, segments=None):
        self.segments = segments
        self.timestamp = 0  # in ms

    def data(self):
        """
        Generator to iterate over the segments in this stream. Duplicated
        segments are ignored.
        """
        seqs = set()
        while not self.segments.empty():
            (self.timestamp, tcp_pkt) = self.segments.get()[1]
            if tcp_pkt.seq in seqs:
                continue
            yield tcp_pkt.data
            seqs.add(tcp_pkt.seq)


class Cache(object):
    """
    Implements caching mechanic to cache messages from pcap file in Redis.
    """
    def __init__(self, filepath):
        self.filepath = filepath
        self.redis_pipe = REDIS_CONN.pipeline()
        self.serializer = Serializer()
        self.streams = defaultdict(PriorityQueue)
        self.stream = Stream()
        self.count = 0
        self.keys = set()  # ping:ADDRESS-PORT:NONCE

    def cache_messages(self):
        """
        Reconstructs messages from TCP streams and caches them in Redis.
        """
        self.extract_streams()
        for stream_id, self.stream.segments in self.streams.iteritems():
            data = self.stream.data()
            _data = data.next()
            while True:
                try:
                    (msg, _data) = self.serializer.deserialize_msg(_data)
                except (HeaderTooShortError, PayloadTooShortError) as err:
                    logging.debug("%s: %s", stream_id, err)
                    try:
                        _data += data.next()
                    except StopIteration:
                        break
                except ProtocolError as err:
                    logging.debug("%s: %s", stream_id, err)
                    try:
                        _data = data.next()
                    except StopIteration:
                        break
                else:
                    node = (stream_id[0], stream_id[1])
                    self.cache_message(node, self.stream.timestamp, msg)
        self.redis_pipe.execute()
        self.cache_rtt()

    def extract_streams(self):
        """
        Extracts TCP streams with data from the pcap file. TCP segments in
        each stream are queued according to their sequence number.
        """
        with open(self.filepath) as pcap_file:
            pcap_reader = dpkt.pcap.Reader(pcap_file)
            for timestamp, buf in pcap_reader:
                frame = dpkt.ethernet.Ethernet(buf)
                ip_pkt = frame.data
                if isinstance(ip_pkt.data, dpkt.tcp.TCP):
                    ip_ver = socket.AF_INET
                    if ip_pkt.v == 6:
                        ip_ver = socket.AF_INET6
                    tcp_pkt = ip_pkt.data
                    stream_id = (
                        socket.inet_ntop(ip_ver, ip_pkt.src),
                        tcp_pkt.sport,
                        socket.inet_ntop(ip_ver, ip_pkt.dst),
                        tcp_pkt.dport
                    )
                    if len(tcp_pkt.data) > 0:
                        timestamp = int(timestamp * 1000)  # in ms
                        self.streams[stream_id].put(
                            (tcp_pkt.seq, (timestamp, tcp_pkt)))
        logging.info("Streams: %d", len(self.streams))

    def cache_message(self, node, timestamp, msg):
        """
        Caches inv/pong message from the specified node.
        """
        if msg['command'] == "inv":
            for inv in msg['inventory']:
                key = "inv:{}:{}".format(inv['type'], inv['hash'])
                if inv['type'] == 2:
                    # Redis key for reference (first seen) block inv
                    rkey = "r{}".format(key)
                    rkey_ms = REDIS_CONN.get(rkey)
                    if rkey_ms is None:
                        REDIS_CONN.set(rkey, timestamp)
                        self.redis_pipe.set("lastblockhash", inv['hash'])
                    elif (timestamp - int(rkey_ms)) / 1000 > SETTINGS['ttl']:
                        # Ignore block inv first seen more than 3 hours ago
                        logging.debug("Skip: %s", key)
                        continue
                self.redis_pipe.zadd(key, timestamp, node)
                self.redis_pipe.expire(key, SETTINGS['ttl'])
            self.count += msg['count']
        elif msg['command'] == "pong":
            key = "ping:{}-{}:{}".format(node[0], node[1], msg['nonce'])
            self.redis_pipe.rpushx(key, timestamp)
            self.keys.add(key)
            self.count += 1

    def cache_rtt(self):
        """
        Calculates round-trip time (RTT) values and caches them in Redis.
        """
        for key in self.keys:
            timestamps = REDIS_CONN.lrange(key, 0, 1)
            if len(timestamps) > 1:
                rtt_key = "rtt:{}".format(':'.join(key.split(":")[1:-1]))
                rtt = int(timestamps[1]) - int(timestamps[0])  # pong - ping
                logging.debug("%s: %d", rtt_key, rtt)
                self.redis_pipe.lpush(rtt_key, rtt)
                self.redis_pipe.ltrim(rtt_key, 0, SETTINGS['rtt_count'] - 1)
                self.redis_pipe.expire(rtt_key, SETTINGS['ttl'])
        self.redis_pipe.execute()


def cron():
    """
    Periodically fetches oldest pcap file to extract messages from.
    """
    while True:
        time.sleep(1)

        try:
            oldest = min(glob.iglob("{}/*.pcap".format(SETTINGS['pcap_dir'])))
        except ValueError as err:
            logging.warning(err)
            continue
        latest = max(glob.iglob("{}/*.pcap".format(SETTINGS['pcap_dir'])))
        if oldest == latest:
            continue
        tmp = oldest
        dump = tmp.replace(".pcap", ".pcap_")
        try:
            os.rename(tmp, dump)  # Mark file as being read
        except OSError as err:
            logging.warning(err)
            continue

        logging.info("Loading: %s", dump)

        start = time.time()
        cache = Cache(filepath=dump)
        cache.cache_messages()
        end = time.time()
        elapsed = end - start

        logging.info("Dump: %s (%d messages)", dump, cache.count)
        logging.info("Elapsed: %d", elapsed)

        os.remove(dump)


def init_settings(argv):
    """
    Populates SETTINGS with key-value pairs from configuration file.
    """
    conf = ConfigParser()
    conf.read(argv[1])
    SETTINGS['logfile'] = conf.get('pcap', 'logfile')
    SETTINGS['debug'] = conf.getboolean('pcap', 'debug')
    SETTINGS['ttl'] = conf.getint('pcap', 'ttl')
    SETTINGS['rtt_count'] = conf.getint('pcap', 'rtt_count')
    SETTINGS['pcap_dir'] = conf.get('pcap', 'pcap_dir')
    if not os.path.exists(SETTINGS['pcap_dir']):
        os.makedirs(SETTINGS['pcap_dir'])


def main(argv):
    if len(argv) < 2 or not os.path.exists(argv[1]):
        print("Usage: pcap.py [config]")
        return 1

    # Initialize global settings
    init_settings(argv)

    # Initialize logger
    loglevel = logging.INFO
    if SETTINGS['debug']:
        loglevel = logging.DEBUG

    logformat = ("%(asctime)s,%(msecs)05.1f %(levelname)s (%(funcName)s) "
                 "%(message)s")
    logging.basicConfig(level=loglevel,
                        format=logformat,
                        filename=SETTINGS['logfile'],
                        filemode='a')
    print("Writing output to {}, press CTRL+C to terminate..".format(
        SETTINGS['logfile']))

    cron()

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
