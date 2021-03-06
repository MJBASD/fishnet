#!/usr/bin/env python
# -*- coding: utf-8 -*-

# This file is part of the lichess.org fishnet client.
# Copyright (C) 2016 Niklas Fiekas <niklas.fiekas@backscattering.de>
#
# The MIT license
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software
# is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""Distributed Stockfish analysis for lichess.org"""

from __future__ import print_function
from __future__ import division

import argparse
import logging
import json
import time
import random
import collections
import contextlib
import multiprocessing
import threading
import site
import sys
import os
import stat
import math
import platform
import re
import textwrap
import getpass
import signal
import ctypes
import string

from distutils.version import LooseVersion

if os.name == "posix" and sys.version_info[0] < 3:
    try:
        import subprocess32 as subprocess
    except ImportError:
        import subprocess
else:
    import subprocess

try:
    import httplib
except ImportError:
    import http.client as httplib

try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse

try:
    import urllib.request as urllib
except ImportError:
    import urllib

try:
    import configparser
except ImportError:
    import ConfigParser as configparser

try:
    from shlex import quote as shell_quote
except ImportError:
    from pipes import quote as shell_quote

try:
    input = raw_input
except NameError:
    pass


__version__ = "1.6.3"

__author__ = "Niklas Fiekas"
__email__ = "niklas.fiekas@backscattering.de"
__license__ = "MIT"

DEFAULT_ENDPOINT = "https://en.lichess.org/fishnet/"
DEFAULT_THREADS = 4
HASH_MIN = 16
HASH_DEFAULT = 256
HASH_MAX = 512
MAX_BACKOFF = 30.0
MAX_FIXED_BACKOFF = 3.0
HTTP_TIMEOUT = 15.0
STAT_INTERVAL = 60.0
DEFAULT_CONFIG = "fishnet.ini"
PROGRESS_REPORT_INTERVAL=3.0
CHECK_PYPI_CHANCE = 0.01


def intro():
    return r"""
.   _________         .    .
.  (..       \_    ,  |\  /|
.   \       O  \  /|  \ \/ /
.    \______    \/ |   \  /      _____ _     _     _   _      _
.       vvvv\    \ |   /  |     |  ___(_)___| |__ | \ | | ___| |_
.       \^^^^  ==   \_/   |     | |_  | / __| '_ \|  \| |/ _ \ __|
.        `\_   ===    \.  |     |  _| | \__ \ | | | |\  |  __/ |_
.        / /\_   \ /      |     |_|   |_|___/_| |_|_| \_|\___|\__| %s
.        |/   \_  \|      /
.               \________/      Distributed Stockfish analysis for lichess.org
""" % __version__


PROGRESS = 15
ENGINE = 5
logging.addLevelName(PROGRESS, "PROGRESS")
logging.addLevelName(ENGINE, "ENGINE")


class LogFormatter(logging.Formatter):
    def format(self, record):
        # Format message
        msg = super(LogFormatter, self).format(record)

        # Add level name
        if record.levelno in [logging.INFO, PROGRESS]:
            with_level = msg
        else:
            with_level = "%s: %s" % (record.levelname, msg)

        # Add thread name
        if record.threadName == "MainThread":
            return with_level
        else:
            return "%s: %s" % (record.threadName, with_level)


class CollapsingLogHandler(logging.StreamHandler):
    def __init__(self, stream=sys.stdout):
        super(CollapsingLogHandler, self).__init__(stream)
        self.last_level = logging.INFO
        self.last_len = 0

    def emit(self, record):
        try:
            if self.last_level == PROGRESS:
                if record.levelno == PROGRESS:
                    self.stream.write("\r")
                else:
                    self.stream.write("\n")

            msg = self.format(record)
            if record.levelno == PROGRESS:
                self.stream.write(msg.ljust(self.last_len))
                self.last_len = max(len(msg), self.last_len)
            else:
                self.last_len = 0
                self.stream.write(msg)
                self.stream.write("\n")

            self.last_level = record.levelno
            self.flush()
        except Exception:
            self.handleError(record)


class TailLogHandler(logging.Handler):
    def __init__(self, capacity, max_level, flush_level, target_handler):
        super(TailLogHandler, self).__init__()
        self.buffer = collections.deque(maxlen=capacity)
        self.max_level = max_level
        self.flush_level = flush_level
        self.target_handler = target_handler

    def emit(self, record):
        if record.levelno < self.max_level:
            self.buffer.append(record)

        if record.levelno >= self.flush_level:
            while self.buffer:
                record = self.buffer.popleft()
                self.target_handler.handle(record)


class CensorLogFilter(logging.Filter):
    def __init__(self, keyword):
        self.keyword = keyword

    def censor(self, msg):
        try:
            # Python 2
            if not isinstance(msg, basestring):
                return msg
        except NameError:
            # Python 3
            if not isinstance(msg, str):
                return msg

        if self.keyword:
            return msg.replace(self.keyword, "*" * len(self.keyword))
        else:
            return msg

    def filter(self, record):
        record.msg = self.censor(record.msg)
        record.args = tuple(self.censor(arg) for arg in record.args)
        return True


def setup_logging(verbosity, stream=sys.stdout):
    logger = logging.getLogger()
    logger.setLevel(ENGINE)

    handler = logging.StreamHandler(stream)

    if verbosity >= 3:
        handler.setLevel(ENGINE)
    elif verbosity >= 2:
        handler.setLevel(logging.DEBUG)
    elif verbosity >= 1:
        handler.setLevel(PROGRESS)
    else:
        if stream.isatty():
            handler = CollapsingLogHandler(stream)
            handler.setLevel(PROGRESS)
        else:
            handler.setLevel(logging.INFO)

    tail_target = logging.StreamHandler(stream)
    tail_target.setFormatter(LogFormatter())
    logger.addHandler(TailLogHandler(25, handler.level, logging.ERROR, tail_target))

    handler.setFormatter(LogFormatter())
    logger.addHandler(handler)


def base_url(url):
    url_info = urlparse.urlparse(url)
    return "%s://%s/" % (url_info.scheme, url_info.hostname)


class HttpError(Exception):
    def __init__(self, status, reason, body):
        self.status = status
        self.reason = reason
        self.body = body

    def __str__(self):
        return "HTTP %d %s\n\n%s" % (self.status, self.reason, self.body)

    def __repr__(self):
        return "%s(%d, %r, %r)" % (type(self).__name__, self.status,
                                   self.reason, self.body)


class HttpServerError(HttpError):
    pass


class HttpClientError(HttpError):
    pass


@contextlib.contextmanager
def http(method, url, body=None, headers=None):
    logging.debug("HTTP request: %s %s, body: %s", method, url, body)

    url_info = urlparse.urlparse(url)
    if url_info.scheme == "https":
        con = httplib.HTTPSConnection(url_info.hostname, url_info.port or 443,
                                      timeout=HTTP_TIMEOUT)
    else:
        con = httplib.HTTPConnection(url_info.hostname, url_info.port or 80,
                                     timeout=HTTP_TIMEOUT)

    headers_with_useragent = {"User-Agent": "fishnet %s" % __version__}
    if headers:
        headers_with_useragent.update(headers)

    con.request(method, url_info.path, body, headers_with_useragent)
    response = con.getresponse()
    logging.debug("HTTP response: %d %s", response.status, response.reason)

    try:
        if 400 <= response.status < 500:
            raise HttpClientError(response.status, response.reason,
                                  response.read())
        elif 500 <= response.status < 600:
            raise HttpServerError(response.status, response.reason,
                                  response.read())
        else:
            yield response
    finally:
        con.close()


class ConfigError(Exception):
    pass


class UpdateRequired(Exception):
    pass


class Shutdown(Exception):
    pass


class SignalHandler(object):
    def __init__(self):
        self.ignore = False

    def install(self):
        signal.signal(signal.SIGTERM, self.handle_term)
        signal.signal(signal.SIGINT, self.handle_term)

        try:
            signal.signal(signal.SIGUSR1, self.handle_usr1)
        except AttributeError:
            # No SIGUSR1 on Windows
            pass

    def handle_term(self, signum, frame):
        if not self.ignore:
            self.ignore = True
            raise Shutdown()

    def handle_usr1(self, signum, frame):
        if not self.ignore:
            self.ignore = True
            raise UpdateRequired()


def open_process(command, cwd=None, shell=True, _popen_lock=threading.Lock()):
    kwargs = {
        "shell": shell,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.PIPE,
        "bufsize": 1,  # Line buffered
        "universal_newlines": True,
    }

    if cwd is not None:
        kwargs["cwd"] = cwd

    # Prevent signal propagation from parent process
    try:
        # Windows
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    except AttributeError:
        # Unix
        kwargs["preexec_fn"] = os.setpgrp

    with _popen_lock:  # Work around Python 2 Popen race condition
        return subprocess.Popen(command, **kwargs)


def kill_process(p):
    try:
        # Windows
        p.send_signal(signal.CTRL_BREAK_EVENT)
    except AttributeError:
        # Unix
        os.killpg(p.pid, signal.SIGKILL)


def send(p, line):
    logging.log(ENGINE, "%s << %s", p.pid, line)
    p.stdin.write(line)
    p.stdin.write("\n")
    p.stdin.flush()


def recv(p):
    while True:
        line = p.stdout.readline()
        if line == "":
            raise EOFError()
        line = line.rstrip()

        logging.log(ENGINE, "%s >> %s", p.pid, line)

        command_and_args = line.split(None, 1)
        if len(command_and_args) == 1:
            return command_and_args[0], ""
        elif len(command_and_args) == 2:
            return command_and_args


def uci(p):
    send(p, "uci")

    engine_info = {}
    options = set()

    while True:
        command, arg = recv(p)

        if command == "uciok":
            return engine_info, options
        elif command == "id":
            name_and_value = arg.split(None, 1)
            if len(name_and_value) == 2:
                engine_info[name_and_value[0]] = name_and_value[1]
        elif command == "option":
            name = []
            for token in arg.split(" ")[1:]:
                if name and token == "type":
                    break
                name.append(token)
            options.add(" ".join(name))
        elif command == "Stockfish" and " by " in arg:
            # Ignore identification line
            pass
        else:
            logging.warning("Unexpected engine output: %s %s", command, arg)


def isready(p):
    send(p, "isready")
    while True:
        command, arg = recv(p)
        if command == "readyok":
            break
        else:
            logging.warning("Unexpected engine output: %s %s", command, arg)


def setoption(p, name, value):
    if value is True:
        value = "true"
    elif value is False:
        value = "false"
    elif value is None:
        value = "none"

    send(p, "setoption name %s value %s" % (name, value))


def go(p, position, moves, movetime=None, depth=None, nodes=None):
    send(p, "position fen %s moves %s" % (position, " ".join(moves)))
    isready(p)

    builder = []
    builder.append("go")
    if movetime is not None:
        builder.append("movetime")
        builder.append(str(movetime))
    if depth is not None:
        builder.append("depth")
        builder.append(str(depth))
    if nodes is not None:
        builder.append("nodes")
        builder.append(str(nodes))
    send(p, " ".join(builder))

    info = {}
    info["bestmove"] = None

    while True:
        command, arg = recv(p)

        if command == "bestmove":
            bestmove = arg.split()[0]
            if bestmove and bestmove != "(none)":
                info["bestmove"] = bestmove
            isready(p)
            return info
        elif command == "info":
            arg = arg or ""

            # Parse all other parameters
            score_kind, score_value, score_bound = None, None, False
            current_parameter = None
            for token in arg.split(" "):
                if current_parameter == "string":
                    # Everything until the end of line is a string
                    if "string" in info:
                        info["string"] += " " + token
                    else:
                        info["string"] = token
                elif token == "score":
                    current_parameter = "score"
                elif token == "pv":
                    current_parameter = "pv"
                    if info.get("multipv", 1) == 1:
                        info.pop("pv", None)
                elif token in ["depth", "seldepth", "time", "nodes", "multipv",
                               "currmove", "currmovenumber",
                               "hashfull", "nps", "tbhits", "cpuload",
                               "refutation", "currline", "string"]:
                    current_parameter = token
                    info.pop(current_parameter, None)
                elif current_parameter in ["depth", "seldepth", "time",
                                           "nodes", "currmovenumber",
                                           "hashfull", "nps", "tbhits",
                                           "cpuload", "multipv"]:
                    # Integer parameters
                    info[current_parameter] = int(token)
                elif current_parameter == "score":
                    # Score
                    if token in ["cp", "mate"]:
                        score_kind = token
                        score_value = None
                    elif token in ["lowerbound", "upperbound"]:
                        score_bound = True
                    else:
                        score_value = int(token)
                elif current_parameter != "pv" or info.get("multipv", 1) == 1:
                    # Strings
                    if current_parameter in info:
                        info[current_parameter] += " " + token
                    else:
                        info[current_parameter] = token

            # Set score if not just a bound
            if score_kind and score_value is not None and not score_bound:
                info["score"] = {score_kind: score_value}
        else:
            logging.warning("Unexpected engine output: %s %s", command, arg)


def set_variant_options(p, variant):
    variant = variant.lower()
    setoption(p, "UCI_Chess960", variant in ["fromposition", "chess960"])
    setoption(p, "UCI_Atomic", variant == "atomic")
    setoption(p, "UCI_Horde", variant == "horde")
    setoption(p, "UCI_House", variant == "crazyhouse")
    setoption(p, "UCI_KingOfTheHill", variant == "kingofthehill")
    setoption(p, "UCI_Race", variant == "racingkings")
    setoption(p, "UCI_3Check", variant == "threecheck")


def depth(level):
    if level in [1, 2]:
        return 1
    elif level == 3:
        return 2
    elif level == 4:
        return 3
    elif level == 5:
        return 5
    elif level == 6:
        return 8
    elif level == 7:
        return 13
    elif level == 8:
        return 21
    else:  # Analysis
        return 99


class Worker(threading.Thread):
    def __init__(self, conf, threads):
        super(Worker, self).__init__()
        self.conf = conf
        self.threads = threads

        self.alive = True
        self.fatal_error = None
        self.finished = threading.Event()
        self.sleep = threading.Event()
        self.status_lock = threading.Lock()

        self.nodes = 0
        self.positions = 0

        self.job = None
        self.process = None
        self.engine_info = None
        self.backoff = start_backoff(self.conf)

    def stop(self):
        with self.status_lock:
            self.alive = False

            if self.process:
                kill_process(self.process)

            self.sleep.set()

    def is_alive(self):
        with self.status_lock:
            return self.alive

    def run(self):
        try:
            while self.is_alive():
                self.run_inner()
        except UpdateRequired as error:
            self.fatal_error = error
        except Exception as error:
            self.fatal_error = error
            logging.exception("Fatal error in worker")
        finally:
            self.finished.set()

    def run_inner(self):
        try:
            # Check if engine is still alive
            if self.process:
                self.process.poll()

            # Restart the engine
            if not self.process or self.process.returncode is not None:
                self.start_engine()

            # Do the next work unit
            path, request = self.work()

            # Report result and fetch next job
            with http("POST", get_endpoint(self.conf, path), json.dumps(request)) as response:
                if response.status == 204:
                    self.job = None
                    t = next(self.backoff)
                    logging.debug("No job found. Backing off %0.1fs", t)
                    self.sleep.wait(t)
                else:
                    data = response.read().decode("utf-8")
                    logging.debug("Got job: %s", data)

                    self.job = json.loads(data)
                    self.backoff = start_backoff(self.conf)
        except HttpServerError as err:
            self.job = None
            t = next(self.backoff)
            logging.error("Server error: HTTP %d %s. Backing off %0.1fs", err.status, err.reason, t)
            self.sleep.wait(t)
        except HttpClientError as err:
            self.job = None
            t = next(self.backoff)
            try:
                logging.debug("Client error: HTTP %d %s: %s", err.status, err.reason, err.body.decode("utf-8"))
                error = json.loads(err.body.decode("utf-8"))["error"]
                logging.error(error)

                if "Please restart fishnet to upgrade." in error:
                    logging.error("Stopping worker for update.")
                    raise UpdateRequired()
            except (KeyError, ValueError):
                logging.error("Client error: HTTP %d %s. Backing off %0.1fs. Request was: %s",
                              err.status, err.reason, t, json.dumps(request))
            self.sleep.wait(t)
        except EOFError:
            if not self.is_alive():
                # Abort
                if self.job:
                    logging.debug("Aborting job %s", self.job["work"]["id"])
                    with http("POST", get_endpoint(self.conf, "abort/%s" % self.job["work"]["id"]), json.dumps(self.make_request())) as response:
                        response.read()
                        logging.info("Aborted job %s", self.job["work"]["id"])
            else:
                t = next(self.backoff)
                logging.exception("Engine process has died. Backing off %0.1fs", t)
                self.sleep.wait(t)
                kill_process(self.process)
        except Exception:
            self.job = None
            t = next(self.backoff)
            logging.exception("Backing off %0.1fs after exception in worker", t)
            self.sleep.wait(t)

            # If in doubt, restart engine
            kill_process(self.process)

    def start_engine(self):
        # Start process
        self.process = open_process(get_engine_command(self.conf, False),
                                    get_engine_dir(self.conf))

        self.engine_info, _ = uci(self.process)
        logging.info("Started engine process, pid: %d, "
                     "threads: %d, identification: %s",
                     self.process.pid, self.threads,
                     self.engine_info.get("name", "<none>"))

        # Prepare UCI options
        self.engine_info["options"] = {}
        if self.conf.has_section("Engine"):
            for name, value in self.conf.items("Engine"):
                self.engine_info["options"][name] = value

        self.engine_info["options"]["threads"] = str(self.threads)

        # Set UCI options
        for name, value in self.engine_info["options"].items():
            setoption(self.process, name, value)

        isready(self.process)

    def make_request(self):
        return {
            "fishnet": {
                "version": __version__,
                "python": platform.python_version(),
                "apikey": get_key(self.conf),
            },
            "engine": self.engine_info
        }

    def work(self):
        result = self.make_request()

        if self.job and self.job["work"]["type"] == "analysis":
            result = self.analysis(self.job)
            return "analysis" + "/" + self.job["work"]["id"], result
        elif self.job and self.job["work"]["type"] == "move":
            result = self.bestmove(self.job)
            return "move" + "/" + self.job["work"]["id"], result
        else:
            if self.job:
                logging.error("Invalid job type: %s", self.job["work"]["type"])

            return "acquire", result

    def bestmove(self, job):
        lvl = job["work"]["level"]
        set_variant_options(self.process, job.get("variant", "standard"))
        setoption(self.process, "Skill Level", int(round((lvl - 1) * 20.0 / 7)))
        isready(self.process)

        moves = job["moves"].split(" ")

        movetime = int(round(4000.0 / (self.threads * 0.9 ** (self.threads - 1)) / 10.0 * lvl / 8.0))

        logging.log(PROGRESS, "Playing %s%s with level %d and movetime %d ms",
                    base_url(get_endpoint(self.conf)), job["game_id"],
                    lvl, movetime)

        part = go(self.process, job["position"], moves,
                  movetime=movetime, depth=depth(lvl))

        self.nodes += part.get("nodes", 0)
        self.positions += 1

        result = self.make_request()
        result["move"] = {
            "bestmove": part["bestmove"],
        }
        return result

    def send_analysis_progress(self, job, result):
        path = "analysis/%s" % job["work"]["id"]

        try:
            with http("POST", get_endpoint(self.conf, path), json.dumps(result)) as response:
                if response.status != 204:
                    logging.error("Expected status 204 for progress report, got %d", response.status)
            return True
        except:
            logging.exception("Could not send progress report. Continuing.")
            return False

    def analysis(self, job, progress_report_interval=PROGRESS_REPORT_INTERVAL):
        set_variant_options(self.process, job.get("variant", "standard"))
        setoption(self.process, "Skill Level", 20)
        isready(self.process)

        send(self.process, "ucinewgame")
        isready(self.process)

        moves = job["moves"].split(" ")

        result = self.make_request()
        result["analysis"] = [None for _ in range(len(moves) + 1)]
        start = last_progress_report = time.time()

        for ply in range(len(moves), -1, -1):
            if last_progress_report + progress_report_interval < time.time():
                if self.send_analysis_progress(job, result):
                    last_progress_report = time.time()

            logging.log(PROGRESS, "Analysing %s%s#%d",
                        base_url(get_endpoint(self.conf)), job["game_id"], ply)

            part = go(self.process, job["position"], moves[0:ply],
                      nodes=3000000, movetime=4000)

            if "mate" not in part["score"] and "time" in part and part["time"] < 100:
                logging.warning("Very low time reported: %d ms.", part["time"])

            if "nps" in part and part["nps"] >= 100000000:
                logging.warning("Dropping exorbitant nps: %d", part["nps"])
                del part["nps"]

            self.nodes += part.get("nodes", 0)
            self.positions += 1

            result["analysis"][ply] = part

        end = time.time()
        logging.info("%s%s took %0.1fs (%0.2fs per position)",
                     base_url(get_endpoint(self.conf)), job["game_id"],
                     end - start, (end - start) / (len(moves) + 1))

        return result


def detect_cpu_capabilities():
    # Detects support for popcnt and pext instructions
    modern, bmi2 = False, False

    # Run cpuid in subprocess for robustness in case of segfaults
    cmd = []
    cmd.append(sys.executable)
    if __package__ is not None:
        cmd.append("-m")
        cmd.append(os.path.splitext(os.path.basename(__file__))[0])
    else:
        cmd.append(__file__)
    cmd.append("cpuid")

    process = open_process(cmd, shell=False)

    # Parse output
    while True:
        line = process.stdout.readline()
        if not line:
            break

        line = line.rstrip()
        logging.debug("cpuid >> %s", line)
        if not line:
            continue

        columns = line.split()
        if columns[0] == "CPUID":
            pass
        elif len(columns) == 5 and all(all(c in string.hexdigits for c in col) for col in columns):
            eax, a, b, c, d = [int(col, 16) for col in columns]

            # popcnt
            if eax == 1 and c & (1 << 23):
                modern = True

            # pext
            if eax == 7 and b & (1 << 8):
                bmi2 = True
        else:
            logging.warning("Unexpected cpuid output: %s", line)

    # Done
    process.wait()
    if process.returncode != 0:
        logging.error("cpuid exited with status code %d", process.returncode)

    return modern, bmi2


def stockfish_filename():
    machine = platform.machine().lower()

    modern, bmi2 = detect_cpu_capabilities()
    if modern and bmi2:
        suffix = "-bmi2"
    elif modern:
        suffix = "-modern"
    else:
        suffix = ""

    if os.name == "nt":
        return "stockfish-windows-%s%s.exe" % (machine, suffix)
    elif os.name == "os2" or sys.platform == "darwin":
        return "stockfish-osx-%s" % machine
    elif os.name == "posix":
        return "stockfish-%s%s" % (machine, suffix)


def update_stockfish(conf, filename):
    path = os.path.join(get_engine_dir(conf), filename)
    logging.info("Engine target path: %s", path)

    headers = {}

    # Only update to newer versions
    try:
        headers["If-Modified-Since"] = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(os.path.getmtime(path)))
    except OSError:
        pass

    # Escape GitHub API rate limiting
    if "GITHUB_API_TOKEN" in os.environ:
        headers["Authorization"] = "token %s" % os.environ["GITHUB_API_TOKEN"]

    # Find latest release
    logging.info("Looking up %s ...", filename)

    with http("GET", "https://api.github.com/repos/niklasf/Stockfish/releases/latest", headers=headers) as response:
        if response.status == 304:
            logging.info("Local %s is newer than release", filename)
            return filename

        release = json.loads(response.read().decode("utf-8"))

    logging.info("Latest stockfish release is tagged %s", release["tag_name"])

    for asset in release["assets"]:
        if asset["name"] == filename:
            logging.info("Found %s" % asset["browser_download_url"])
            break
    else:
        raise ConfigError("No precompiled %s for your platform" % filename)

    # Download
    logging.info("Downloading %s ...", filename)

    def reporthook(a, b, c):
        if sys.stderr.isatty():
            sys.stderr.write("\rDownloading %s: %d/%d (%d%%)" % (
                                 filename, min(a * b, c), c,
                                 round(min(a * b, c) * 100 / c)))
            sys.stderr.flush()

    urllib.urlretrieve(asset["browser_download_url"], path, reporthook)

    sys.stderr.write("\n")
    sys.stderr.flush()

    # Make executable
    logging.info("chmod +x %s", filename)
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IEXEC)
    return filename


def is_user_site_package():
    try:
        user_site = site.getusersitepackages()
    except AttributeError:
        return False

    return os.path.abspath(__file__).startswith(os.path.join(user_site, ""))


def update_self(force=False):
    # Ensure current instance is installed as a package
    if __package__ is None:
        raise ConfigError("Not started as a package (python -m). Can not update using pip")

    if all(dirname not in ["site-packages", "dist-packages"] for dirname in __file__.split(os.sep)):
        raise ConfigError("Not installed as package (%s). Can not update using pip" % __file__)

    logging.debug("Package: \"%s\", name: %s, loader: %s",
                  __package__, __name__, __loader__)

    # Ensure pip is available
    try:
        import pip
    except ImportError:
        raise ConfigError("Auto update enabled, but pip not installed")

    # Check pip is up to date
    pip_up_to_date = LooseVersion(pip.__version__) >= LooseVersion("8.0.0")
    if pip_up_to_date:
        logging.debug("using pip %s", pip.__version__)
    else:
        logging.warning("using pip %s < 8.0.0", pip.__version__)

    # Ensure module file is going to be writable
    try:
        with open(__file__, "r+"):
            pass
    except IOError:
        raise ConfigError("Auto update enabled, but no write permissions "
                          "to module file. Use virtualenv or "
                          "pip install --user")

    # Check for updates
    if not update_available():
        if not force:
            return 0
        else:
            logging.info("Updating anyway")

    # Wait
    t = random.random() * 15.0
    logging.info("Waiting %0.1fs before update ...", t)
    time.sleep(t)

    print()

    # Force download
    if force:
        if pip_up_to_date:
            logging.info("$ pip download fishnet")
            ret = pip.main(["download", "fishnet"])
        else:
            logging.info("$ pip install --download fishnet")
            ret = pip.main(["install", "--download", "fishnet"])

        if ret != 0:
            logging.warning("Unexpected exit code for pip download: %d", ret)
            return ret

        print()

    # Check for root privileges
    try:
        is_root = os.geteuid() == 0
    except AttributeError:
        # No os.geteuid() on Windows
        is_root = False

    # Update
    if is_user_site_package():
        logging.info("$ pip install --user --upgrade fishnet")
        ret = pip.main(["install", "--user", "--upgrade", "fishnet"])
    else:
        logging.info("$ pip install --upgrade fishnet")
        ret = pip.main(["install", "--upgrade", "fishnet"])
    if ret != 0:
        logging.warning("Unexpected exit code for pip install: %d", ret)
        return ret

    print()

    # Wait
    t = random.random() * 15.0
    logging.info("Waiting %0.1fs before respawn ...", t)
    time.sleep(t)

    # Respawn
    argv = []
    argv.append(sys.executable)
    argv.append("-m")
    argv.append(os.path.splitext(os.path.basename(__file__))[0])
    argv += sys.argv[1:]

    logging.debug("Restarting with execv: %s, argv: %s",
                  sys.executable, " ".join(argv))

    os.execv(sys.executable, argv)


def load_conf(args):
    conf = configparser.ConfigParser()
    conf.add_section("Fishnet")
    conf.add_section("Engine")

    if not args.no_conf:
        if not args.conf and not os.path.isfile(DEFAULT_CONFIG):
            return configure(args)

        config_file = args.conf or DEFAULT_CONFIG
        logging.debug("Using config file: %s", config_file)

        if not conf.read(config_file):
            raise ConfigError("Could not read config file: %s" % config_file)

    if hasattr(args, "engine_dir") and args.engine_dir is not None:
        conf.set("Fishnet", "EngineDir", args.engine_dir)
    if hasattr(args, "engine_command") and args.engine_command is not None:
        conf.set("Fishnet", "EngineCommand", args.engine_command)
    if hasattr(args, "key") and args.key is not None:
        conf.set("Fishnet", "Key", args.key)
    if hasattr(args, "cores") and args.cores is not None:
        conf.set("Fishnet", "Cores", args.cores)
    if hasattr(args, "memory") and args.memory is not None:
        conf.set("Fishnet", "Memory", args.memory)
    if hasattr(args, "threads") and args.threads is not None:
        conf.set("Fishnet", "Threads", str(args.threads))
    if hasattr(args, "endpoint") and args.endpoint is not None:
        conf.set("Fishnet", "Endpoint", args.endpoint)
    if hasattr(args, "fixed_backoff") and args.fixed_backoff is not None:
        conf.set("Fishnet", "FixedBackoff", str(args.fixed_backoff))

    logging.getLogger().addFilter(CensorLogFilter(conf_get(conf, "Key")))

    return conf


def config_input(prompt, validator, out):
    while True:
        if out == sys.stdout:
            inp = input(prompt)
        else:
            if prompt:
                out.write(prompt)
                out.flush()

            inp = input()

        try:
            return validator(inp)
        except ConfigError as error:
            print(error, file=out)


def configure(args):
    if sys.stdout.isatty():
        out = sys.stdout
        import readline
    else:
        out = sys.stderr

    print(file=out)
    print("### Configuration", file=out)
    print(file=out)

    conf = configparser.ConfigParser()
    conf.add_section("Fishnet")
    conf.add_section("Engine")

    # Ensure the config file is going to be writable
    config_file = os.path.abspath(args.conf or DEFAULT_CONFIG)
    if os.path.isfile(config_file):
        conf.read(config_file)
        with open(config_file, "r+"):
            pass
    else:
        with open(config_file, "w"):
            pass
        os.remove(config_file)

    # Stockfish working directory
    engine_dir = config_input("Stockfish working directory (default: %s): " % os.path.abspath("."),
                              validate_engine_dir, out)
    conf.set("Fishnet", "EngineDir", engine_dir)

    # Stockfish command
    print(file=out)
    print("Fishnet uses a custom Stockfish build with variant support.", file=out)
    print("Stockfish is licensed under the GNU General Public License v3.", file=out)
    print("You can find the source at: https://github.com/ddugovic/Stockfish", file=out)
    print(file=out)
    print("You can build lichess.org custom Stockfish yourself and provide", file=out)
    print("the path or automatically download a precompiled binary.", file=out)
    print(file=out)
    engine_command = config_input("Path or command (default: download): ",
                                  lambda v: validate_engine_command(v, conf),
                                  out)
    print(file=out)

    # Cores
    max_cores = multiprocessing.cpu_count()
    default_cores = max(1, max_cores - 1)
    cores = config_input("Number of cores to use for engine threads (default %d, max %d): " % (default_cores, max_cores),
                         validate_cores, out)
    conf.set("Fishnet", "Cores", str(cores))

    # Threads
    default_threads = min(DEFAULT_THREADS, cores)
    threads = config_input("Number of threads to use per engine process (default %d, max %d): " % (default_threads, cores),
                           lambda v: validate_threads(v, conf), out)
    conf.set("Fishnet", "Threads", str(threads))

    # Memory
    processes = math.ceil(cores / threads)
    min_memory = HASH_MIN * processes
    default_memory = HASH_DEFAULT * processes
    max_memory = HASH_MAX * processes
    memory = config_input("Memory in MB to use for engine hashtables (default %d, min %d, max %d): " % (default_memory, min_memory, max_memory),
                          lambda v: validate_memory(v, conf), out)
    conf.set("Fishnet", "Memory", str(memory))

    # Advanced options
    endpoint = DEFAULT_ENDPOINT
    fixed_backoff = False
    if config_input("Configure advanced options? (default: no) ", parse_bool, out):
        endpoint = config_input("Fishnet API endpoint (default: %s): " % (endpoint, ), validate_endpoint, out)
        fixed_backoff = config_input("Fixed backoff? (for move servers, default: no) ", parse_bool, out)

    conf.set("Fishnet", "Endpoint", endpoint)
    conf.set("Fishnet", "FixedBackoff", str(fixed_backoff))

    # Change key?
    key = None
    if conf.has_option("Fishnet", "Key"):
        if not config_input("Change fishnet key? (default: no) ", parse_bool, out):
            key = conf.get("Fishnet", "Key")

    # Key
    if key is None:
        status = "required" if is_production_endpoint(conf) else "probably not required"
        key = config_input("Personal fishnet key (append ! to force, %s): " % status,
                           lambda v: validate_key(v, conf, network=True), out)
    conf.set("Fishnet", "Key", key)
    logging.getLogger().addFilter(CensorLogFilter(key))

    # Confirm
    print(file=out)
    while not config_input("Done. Write configuration to %s now? (default: yes) " % (config_file, ),
                           lambda v: parse_bool(v, True), out):
        pass

    # Write configuration
    with open(config_file, "w") as f:
        conf.write(f)

    print("Configuration saved.", file=out)
    return conf


def validate_engine_dir(engine_dir):
    if not engine_dir or not engine_dir.strip():
        return os.path.abspath(".")

    engine_dir = os.path.abspath(os.path.expanduser(engine_dir.strip()))

    if not os.path.isdir(engine_dir):
        raise ConfigError("EngineDir not found: %s" % engine_dir)

    return engine_dir


def validate_engine_command(engine_command, conf):
    if not engine_command or not engine_command.strip() or engine_command.strip().lower() == "download":
        return None

    engine_command = engine_command.strip()
    engine_dir = get_engine_dir(conf)

    # Ensure the required options are supported
    process = open_process(engine_command, engine_dir)
    _, options = uci(process)
    kill_process(process)

    logging.debug("Supported options: %s", ", ".join(options))

    required_options = set(["UCI_Chess960", "UCI_Atomic", "UCI_Horde",
                            "UCI_House", "UCI_KingOfTheHill", "UCI_Race",
                            "UCI_3Check", "Threads", "Hash"])

    missing_options = required_options.difference(options)
    if missing_options:
        raise ConfigError("Ensure you are using lichess custom Stockfish. "
                          "Unsupported engine options: %s" % ", ".join(missing_options))

    return engine_command


def parse_bool(inp, default=False):
    if not inp:
        return default

    inp = inp.strip().lower()
    if not inp:
        return default

    if inp in ["y", "j", "yes", "yep", "true", "t", "1", "ok"]:
        return True
    elif inp in ["n", "no", "nop", "nope", "f", "false", "0"]:
        return False
    else:
        raise ConfigError("Not a boolean value: %s", inp)


def validate_cores(cores):
    if not cores or cores.strip().lower() == "auto":
        return max(1, multiprocessing.cpu_count() - 1)

    if cores.strip().lower() == "all":
        return multiprocessing.cpu_count()

    try:
        cores = int(cores.strip())
    except ValueError:
        raise ConfigError("Number of cores must be an integer")

    if cores < 1:
        raise ConfigError("Need at least one core")

    if cores > multiprocessing.cpu_count():
        raise ConfigError("At most %d cores available on your machine " % multiprocessing.cpu_count())

    return cores


def validate_threads(threads, conf):
    cores = validate_cores(conf_get(conf, "Cores"))

    if not threads or str(threads).strip().lower() == "auto":
        return min(DEFAULT_THREADS, cores)

    try:
        threads = int(str(threads).strip())
    except ValueError:
        raise ConfigError("Number of threads must be an integer")

    if threads < 1:
        raise ConfigError("Need at least one thread per engine process")

    if threads > cores:
        raise ConfigError("%d cores is not enough to run %d threads" % (cores, threads))

    return threads


def validate_memory(memory, conf):
    cores = validate_cores(conf_get(conf, "Cores"))
    threads = validate_threads(conf_get(conf, "Threads"), conf)
    processes = int(math.ceil(cores / threads))

    if not memory or not memory.strip() or memory.strip().lower() == "auto":
        return processes * HASH_DEFAULT

    try:
        memory = int(memory.strip())
    except ValueError:
        raise ConfigError("Memory must be an integer")

    if memory < processes * HASH_MIN:
        raise ConfigError("Not enough memory for a minimum of %d x %d MB in hash tables" % (processes, HASH_MIN))

    if memory > processes * HASH_MAX:
        raise ConfigError("Can not reasonably use more than %d x %d MB = %d MB for hash tables" % (processes, HASH_MAX, processes * HASH_MAX))

    return memory


def validate_endpoint(endpoint):
    if not endpoint or not endpoint.strip():
        return DEFAULT_ENDPOINT

    if not endpoint.endswith("/"):
        endpoint += "/"

    url_info = urlparse.urlparse(endpoint)
    if url_info.scheme not in ["http", "https"]:
        raise ConfigError("Endpoint does not have http:// or https:// URL scheme")

    return endpoint


def validate_key(key, conf, network=False):
    if not key or not key.strip():
        if is_production_endpoint(conf):
            raise ConfigError("Fishnet key required")
        else:
            return ""

    key = key.strip()

    network = network and not key.endswith("!")
    key = key.rstrip("!").strip()

    if not re.match(r"^[a-zA-Z0-9]+$", key):
        raise ConfigError("Fishnet key is expected to be alphanumeric")

    if network:
        try:
            with http("GET", get_endpoint(conf, "key/%s" % key)) as response:
                response.read()
        except HttpClientError as error:
            if error.status == 404:
                raise ConfigError("Invalid or inactive fishnet key")
            else:
                raise

    return key


def conf_get(conf, key, default=None, section="Fishnet"):
    if not conf.has_section(section):
        return default
    elif not conf.has_option(section, key):
        return default
    else:
        return conf.get(section, key)


def get_engine_dir(conf):
    return validate_engine_dir(conf_get(conf, "EngineDir"))


def get_engine_command(conf, update=True):
    engine_command = validate_engine_command(conf_get(conf, "EngineCommand"), conf)
    if not engine_command:
        filename = stockfish_filename()
        if update:
            filename = update_stockfish(conf, filename)
        return validate_engine_command(os.path.join(".", filename), conf)
    else:
        return engine_command


def get_endpoint(conf, sub=""):
    return urlparse.urljoin(validate_endpoint(conf_get(conf, "Endpoint")), sub)


def is_production_endpoint(conf):
    endpoint = validate_endpoint(conf_get(conf, "Endpoint"))
    hostname = urlparse.urlparse(endpoint).hostname
    return hostname == "lichess.org" or hostname.endswith(".lichess.org")


def get_key(conf):
    return validate_key(conf_get(conf, "Key"), conf, network=False)


def start_backoff(conf):
    if parse_bool(conf_get(conf, "FixedBackoff")):
        while True:
            yield random.random() * MAX_FIXED_BACKOFF
    else:
        backoff = 1
        while True:
            yield 0.5 * backoff + 0.5 * backoff * random.random()
            backoff = min(backoff + 1, MAX_BACKOFF)


def lookup_latest_version():
    with http("GET", "https://pypi.python.org/pypi/fishnet/json") as response:
        result = json.loads(response.read().decode("utf-8"))

    return result["info"]["version"]


def update_available():
    try:
        latest_version = lookup_latest_version()
    except Exception:
        logging.exception("Failed to check for update on PyPI")
        return False

    if latest_version == __version__:
        logging.info("[fishnet v%s] Client is up to date", __version__)
        return False
    else:
        logging.info("[fishnet v%s] Update available on PyPI: %s",
                     __version__, latest_version)
        return True


def cmd_run(args):
    conf = load_conf(args)

    if args.auto_update:
        print()
        print("### Updating ...")
        print()
        update_self()

    engine_command = validate_engine_command(conf_get(conf, "EngineCommand"), conf)
    if not engine_command:
        print()
        print("### Updating Stockfish ...")
        print()
        engine_command = get_engine_command(conf)

    print()
    print("### Checking configuration ...")
    print()
    print("EngineDir:     %s" % get_engine_dir(conf))
    print("EngineCommand: %s" % engine_command)
    print("Key:           %s" % (("*" * len(get_key(conf))) or "(none)"))

    spare_threads = validate_cores(conf_get(conf, "Cores"))
    print("Cores:         %d" % spare_threads)
    threads_per_process = validate_threads(conf_get(conf, "Threads"), conf)
    print("Threads:       %d (per engine process)" % threads_per_process)
    memory = validate_memory(conf_get(conf, "Memory"), conf)
    print("Memory:        %d MB" % memory)
    endpoint = get_endpoint(conf)
    warning = "" if endpoint.startswith("https://") else " (WARNING: not using https)"
    print("Endpoint:      %s%s" % (endpoint, warning))
    print("FixedBackoff:  %s" % parse_bool(conf_get(conf, "FixedBackoff")))
    print()

    if conf.has_section("Engine") and conf.items("Engine"):
        print("Using custom UCI options is discouraged:")
        for name, value in conf.items("Engine"):
            print(" * %s = %s" % (name, value))
        print()

    print("### Starting workers ...")
    print()

    # Let spare cores exclusively run engine processes
    workers = []
    while spare_threads > threads_per_process:
        workers.append(Worker(conf, threads_per_process))
        spare_threads -= threads_per_process

    # Use the rest of the cores
    if spare_threads > 0:
        workers.append(Worker(conf, spare_threads))

    # Start all threads
    for i, worker in enumerate(workers):
        worker.name = "><> %d" % (i + 1)
        worker.setDaemon(True)
        worker.start()

    # Wait while the workers are running
    try:
        # Let SIGTERM and SIGINT gracefully terminate the program
        handler = SignalHandler()
        handler.install()

        while True:
            # Check worker status
            for worker in workers:
                worker.finished.wait(STAT_INTERVAL / len(workers))
                if worker.fatal_error:
                    raise worker.fatal_error

            # Log stats
            logging.info("[fishnet v%s] Analyzed %d positions, crunched %d million nodes",
                         __version__,
                         sum(worker.positions for worker in workers),
                         int(sum(worker.nodes for worker in workers) / 1000 / 1000))

            # Check for update
            if random.random() <= CHECK_PYPI_CHANCE and update_available() and args.auto_update:
                raise UpdateRequired()
    except Shutdown:
        if any(worker.job for worker in workers):
            logging.info("\n\n### Good bye! Aborting pending jobs ...\n")
        else:
            logging.info("\n\n### Good bye!")
    except UpdateRequired:
        if any(worker.job for worker in workers):
            logging.info("\n\n### Update required! Aborting pending jobs ...\n")
        else:
            logging.info("\n\n### Update required!")
        raise
    finally:
        handler.ignore = True

        # Stop workers
        for worker in workers:
            worker.stop()

        # Wait
        for worker in workers:
            worker.finished.wait()

    return 0


def cmd_configure(args):
    configure(args)
    return 0


def cmd_systemd(args):
    conf = load_conf(args)

    template = textwrap.dedent("""\
        [Unit]
        Description=Fishnet instance
        After=network.target

        [Service]
        ExecStart={start}
        WorkingDirectory={cwd}
        User={user}
        Group={group}
        Nice=5
        CapabilityBoundingSet=
        PrivateTmp=true
        PrivateDevices=true
        DevicePolicy=closed
        ProtectSystem=full
        NoNewPrivileges=true
        Restart=always

        [Install]
        WantedBy=multi-user.target""")

    # Prepare command line arguments
    builder = [shell_quote(sys.executable)]

    if __package__ is None:
        builder.append(shell_quote(os.path.abspath(sys.argv[0])))
    else:
        builder.append("-m")
        builder.append(shell_quote(os.path.splitext(os.path.basename(__file__))[0]))

    if args.no_conf:
        builder.append("--no-conf")
    else:
        config_file = os.path.abspath(args.conf or DEFAULT_CONFIG)
        builder.append("--conf")
        builder.append(shell_quote(config_file))

    if args.key is not None:
        builder.append("--key")
        builder.append(shell_quote(validate_key(args.key, conf)))
    if args.engine_dir is not None:
        builder.append("--engine-dir")
        builder.append(shell_quote(validate_engine_dir(args.engine_dir)))
    if args.engine_command is not None:
        builder.append("--engine-command")
        builder.append(shell_quote(validate_engine_command(args.engine_command, conf)))
    if args.cores is not None:
        builder.append("--cores")
        builder.append(shell_quote(str(validate_cores(args.cores))))
    if args.memory is not None:
        builder.append("--memory")
        builder.append(shell_quote(str(validate_memory(args.memory, conf))))
    if args.threads is not None:
        builder.append("--threads")
        builder.append(shell_quote(str(validate_threads(args.threads, conf))))
    if args.endpoint is not None:
        builder.append("--endpoint")
        builder.append(shell_quote(validate_endpoint(args.endpoint)))
    if args.fixed_backoff is not None:
        builder.append("--fixed-backoff" if args.fixed_backoff else "--no-fixed-backoff")
    if args.auto_update:
        builder.append("--auto-update")

    builder.append("run")

    start = " ".join(builder)

    print(template.format(
        user=getpass.getuser(),
        group=getpass.getuser(),
        cwd=os.path.abspath("."),
        start=start
    ))

    try:
        if os.geteuid() == 0:
            print("\n# WARNING: Running as root is not recommended!", file=sys.stderr)
    except AttributeError:
        # No os.getuid() on Windows
        pass

    if sys.stdout.isatty():
        print("\n# Example usage:", file=sys.stderr)
        print("# python -m fishnet systemd | sudo tee /etc/systemd/system/fishnet.service", file=sys.stderr)
        print("# sudo systemctl enable fishnet.service", file=sys.stderr)
        print("# sudo systemctl start fishnet.service", file=sys.stderr)


@contextlib.contextmanager
def make_cpuid():
    # Loosely based on cpuid.py by Anders Høst, licensed MIT:
    # https://github.com/flababah/cpuid.py

    # Prepare system information
    is_windows = os.name == "nt"
    is_64bit = ctypes.sizeof(ctypes.c_void_p) == 8
    if platform.machine().lower() not in ["amd64", "x86_64", "x86", "i686"]:
        raise OSError("Got no CPUID opcodes for %s" % platform.machine())

    # Struct for return value
    class CPUID_struct(ctypes.Structure):
        _fields_ = [("eax", ctypes.c_uint32),
                    ("ebx", ctypes.c_uint32),
                    ("ecx", ctypes.c_uint32),
                    ("edx", ctypes.c_uint32)]

    # Select kernel32 or libc
    if is_windows:
        if is_64bit:
            libc = ctypes.CDLL("kernel32.dll")
        else:
            libc = ctypes.windll.kernel32
    else:
        libc = ctypes.pythonapi

    # Select opcodes
    if is_64bit:
        if is_windows:
            # Windows x86_64
            # Two first call registers : RCX, RDX
            # Volatile registers       : RAX, RCX, RDX, R8-11
            opc = [
                0x53,                    # push   %rbx
                0x48, 0x89, 0xd0,        # mov    %rdx,%rax
                0x49, 0x89, 0xc8,        # mov    %rcx,%r8
                0x31, 0xc9,              # xor    %ecx,%ecx
                0x0f, 0xa2,              # cpuid
                0x41, 0x89, 0x00,        # mov    %eax,(%r8)
                0x41, 0x89, 0x58, 0x04,  # mov    %ebx,0x4(%r8)
                0x41, 0x89, 0x48, 0x08,  # mov    %ecx,0x8(%r8)
                0x41, 0x89, 0x50, 0x0c,  # mov    %edx,0xc(%r8)
                0x5b,                    # pop    %rbx
                0xc3                     # retq
            ]
        else:
            # Posix x86_64
            # Two first call registers : RDI, RSI
            # Volatile registers       : RAX, RCX, RDX, RSI, RDI, R8-11
            opc = [
                0x53,                    # push   %rbx
                0x48, 0x89, 0xf0,        # mov    %rsi,%rax
                0x31, 0xc9,              # xor    %ecx,%ecx
                0x0f, 0xa2,              # cpuid
                0x89, 0x07,              # mov    %eax,(%rdi)
                0x89, 0x5f, 0x04,        # mov    %ebx,0x4(%rdi)
                0x89, 0x4f, 0x08,        # mov    %ecx,0x8(%rdi)
                0x89, 0x57, 0x0c,        # mov    %edx,0xc(%rdi)
                0x5b,                    # pop    %rbx
                0xc3                     # retq
            ]
    else:
        # CDECL 32 bit
        # Two first call registers : Stack (%esp)
        # Volatile registers       : EAX, ECX, EDX
        opc = [
            0x53,                    # push   %ebx
            0x57,                    # push   %edi
            0x8b, 0x7c, 0x24, 0x0c,  # mov    0xc(%esp),%edi
            0x8b, 0x44, 0x24, 0x10,  # mov    0x10(%esp),%eax
            0x31, 0xc9,              # xor    %ecx,%ecx
            0x0f, 0xa2,              # cpuid
            0x89, 0x07,              # mov    %eax,(%edi)
            0x89, 0x5f, 0x04,        # mov    %ebx,0x4(%edi)
            0x89, 0x4f, 0x08,        # mov    %ecx,0x8(%edi)
            0x89, 0x57, 0x0c,        # mov    %edx,0xc(%edi)
            0x5f,                    # pop    %edi
            0x5b,                    # pop    %ebx
            0xc3                     # ret
        ]

    code_size = len(opc)
    code = (ctypes.c_ubyte * code_size)(*opc)

    if is_windows:
        # Allocate executable memory
        addr = libc.VirtualAlloc(None, code_size, 0x1000, 0x40)
        if not addr:
            raise MemoryError("Could not VirtualAlloc RWX memory")
    else:
        # Allocate memory
        libc.valloc.restype = ctypes.c_void_p
        libc.valloc.argtypes = [ctypes.c_size_t]
        addr = libc.valloc(code_size)
        if not addr:
            raise MemoryError("Could not valloc memory")

        # Make executable
        libc.mprotect.restype = ctypes.c_int
        libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        if 0 != libc.mprotect(addr, code_size, 1 | 2 | 4):
            raise OSError("Failed to set RWX using mprotect")

    # Copy code to allocated executable memory. No need to flush instruction
    # cache for CPUID.
    ctypes.memmove(addr, code, code_size)

    # Create and yield callable
    result = CPUID_struct()
    func_type = ctypes.CFUNCTYPE(None, ctypes.POINTER(CPUID_struct), ctypes.c_uint32)
    func_ptr = func_type(addr)

    def cpuid(eax):
        func_ptr(result, eax)
        return result.eax, result.ebx, result.ecx, result.edx

    yield cpuid

    # Free
    if is_windows:
        libc.VirtualFree(addr, 0, 0x8000)
    else:
        libc.free.restype = None
        libc.free.argtypes = [ctypes.c_void_p]
        libc.free(addr)


def cmd_cpuid(argv):
    with make_cpuid() as cpuid:
        headers = ["CPUID", "EAX", "EBX", "ECX", "EDX"]
        print(" ".join(header.ljust(8) for header in headers).rstrip())

        for eax in [0x0, 0x80000000]:
            highest, _, _, _ = cpuid(eax)
            for eax in range(eax, highest + 1):
                a, b, c, d = cpuid(eax)
                print("%08x %08x %08x %08x %08x" % (eax, a, b, c, d))


def main(argv):
    # Parse command line arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", "-v", default=0, action="count", help="increase verbosity")
    parser.add_argument("--version", action="version", version="fishnet v{0}".format(__version__))
    parser.add_argument("--conf", help="configuration file")
    parser.add_argument("--no-conf", action="store_true", help="do not use a configuration file")

    parser.add_argument("--key", "--apikey", "-k", help="fishnet api key")

    parser.add_argument("--engine-dir", help="engine working directory")
    parser.add_argument("--engine-command", "-e", help="engine command (default: download precompiled Stockfish)")

    parser.add_argument("--cores", help="number of cores to use for engine processes (or auto for n - 1, or all for n)")
    parser.add_argument("--memory", help="total memory (MB) to use for engine hashtables")
    parser.add_argument("--threads", type=int, help="number of threads per engine process (default: 4)")
    parser.add_argument("--endpoint", help="lichess http endpoint")
    parser.add_argument("--fixed-backoff", action="store_true", default=None, help="fixed backoff (only recommended for move servers)")
    parser.add_argument("--no-fixed-backoff", dest="fixed_backoff", action="store_false", default=None)
    parser.add_argument("--auto-update", action="store_true", help="automatically install available updates")

    commands = collections.OrderedDict([
        ("run", cmd_run),
        ("configure", cmd_configure),
        ("systemd", cmd_systemd),
        ("cpuid", cmd_cpuid),
    ])

    parser.add_argument("command", default="run", nargs="?", choices=commands.keys())

    args = parser.parse_args(argv[1:])

    # Setup logging
    setup_logging(args.verbose,
                  sys.stderr if args.command == "systemd" else sys.stdout)

    # Show intro
    if args.command not in ["systemd", "cpuid"]:
        print(intro())

    # Run
    try:
        sys.exit(commands[args.command](args))
    except UpdateRequired:
        if args.auto_update:
            logging.info("\n\n### Updating ...\n")
            update_self(force=True)

        logging.error("Update required. Exiting (status 70)")
        return 70
    except ConfigError:
        logging.exception("Configuration error")
        return 78
    except (KeyboardInterrupt, Shutdown):
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
