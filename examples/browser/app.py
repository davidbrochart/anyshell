"""Browser-provided terminal and an entirely in-memory workspace."""

import os

os.environ["ANYTEXT_THREADS"] = "0"
os.environ["ANYTEXT_COLOR_SYSTEM"] = "truecolor"

import socket

import anyio
from anytext import constants
from anytext.drivers.callback_driver import CallbackDriver

from anyshell import MemoryFileSystem, Session
from anyshell.frontend import ShellApp

terminal_write = globals()["terminal_write"]
browser_ready = globals()["browser_ready"]

assert not constants.THREADS_ENABLED


def forbidden(*args, **kwargs):
    raise AssertionError("Browser shell attempted to use a thread or socket pair")


anyio.to_thread.run_sync = forbidden
socket.socketpair = forbidden
browser_driver = None


def make_driver(app, **options):
    global browser_driver
    browser_driver = CallbackDriver(app, output=terminal_write, **options)
    return browser_driver


def browser_input(data):
    if browser_driver is not None:
        browser_driver.feed_input(data)


def browser_resize(width, height):
    if browser_driver is not None:
        browser_driver.resize(width, height)


class BrowserShell(ShellApp):
    def on_mount(self):
        browser_ready()


session = Session(
    MemoryFileSystem(
        {
            "readme.txt": "Welcome to anyshell!\n",
            "notes/fruits.txt": "pear\napple\nbanana\napricot\n",
        }
    )
)
browser_app = BrowserShell(
    session,
    prompt_position=globals().get("prompt_position", "inline"),
    driver_class=make_driver,
)
