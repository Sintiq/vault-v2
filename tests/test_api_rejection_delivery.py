"""Unauthorized POST delivery through the existing loopback ApiServer seam."""
import http.client
import json
import socket
import time
from contextlib import closing
from threading import Event, Thread

import pytest

from test_watch_timeline import env, server  # existing synthetic public-server fixtures


def test_rejected_post_delivers_401_when_body_arrives_after_headers(server):
    body = b'{"day":"2026-09-24","readings":{"steps":1}}'
    with closing(http.client.HTTPConnection('127.0.0.1', server.port, timeout=2)) as connection:
        connection.putrequest('POST', '/api/health/watch')
        connection.putheader('Content-Type', 'application/json')
        connection.putheader('Content-Length', str(len(body)))
        connection.endheaders()
        time.sleep(0.01)  # reproducible real TCP split, not a substituted server
        connection.send(body)
        response = connection.getresponse()
        assert response.status == 401
        assert json.loads(response.read()) == {"error": "bad key"}
    assert server.api.health.all() == []


@pytest.mark.parametrize('length_headers', [
    '', 'Content-Length: 10\r\n', 'Content-Length: -1\r\n',
    'Content-Length: nope\r\n', 'Content-Length: 99999999999999999999\r\n',
    'Content-Length: 4\r\nContent-Length: 1000000\r\n',
    'Transfer-Encoding: chunked\r\n',
])
def test_refusal_does_not_wait_for_declared_body_and_drain_is_bounded(server, length_headers):
    headers = ('POST /api/health/watch HTTP/1.1\r\nHost: localhost\r\n'
               + length_headers + '\r\n').encode('ascii')
    with socket.create_connection(('127.0.0.1', server.port), timeout=2) as connection:
        connection.sendall(headers)
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 401
        assert json.loads(response.read()) == {"error": "bad key"}
        # Keep the client write side OPEN without sending the promised body.
        # Its EOF cannot be what releases the server; the finite drain must.
        deadline = time.monotonic() + 1
        while server.active_requests and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.active_requests == 0
        response.close()
    assert server.api.health.all() == []


def test_discarded_bytes_never_become_a_second_authorized_request(server):
    body = b'{"day":"2026-09-24","readings":{"steps":1}}'
    first = b'POST /api/health/watch HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}'
    second = ('POST /api/health/watch HTTP/1.1\r\nHost: localhost\r\n'
              f'Authorization: Bearer {server.key}\r\nContent-Length: {len(body)}\r\n'
              'Content-Type: application/json\r\n\r\n').encode('ascii') + body
    with socket.create_connection(('127.0.0.1', server.port), timeout=2) as connection:
        connection.sendall(first + second)
        connection.shutdown(socket.SHUT_WR)
        with http.client.HTTPResponse(connection) as response:
            response.begin()
            assert response.status == 401
            assert json.loads(response.read()) == {"error": "bad key"}
    deadline = time.monotonic() + 1
    while server.active_requests and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.active_requests == 0
    assert server.api.health.all() == []


def test_continuous_slow_body_cannot_extend_the_refusal_drain_deadline(server):
    stop = Event()
    with socket.create_connection(('127.0.0.1', server.port), timeout=2) as connection:
        connection.sendall(b'POST /api/health/watch HTTP/1.1\r\nHost: localhost\r\n'
                           b'Content-Length: 999999999\r\n\r\n')
        response = http.client.HTTPResponse(connection)
        response.begin()
        assert response.status == 401
        assert json.loads(response.read()) == {"error": "bad key"}

        def drip():
            try:
                while not stop.is_set():
                    connection.sendall(b'x')
                    stop.wait(0.002)
            except OSError:
                pass

        sender = Thread(target=drip)
        sender.start()
        try:
            deadline = time.monotonic() + 1
            while server.active_requests and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.active_requests == 0
        finally:
            stop.set()
            sender.join(timeout=3)
            response.close()
        assert not sender.is_alive()
    assert server.api.health.all() == []
