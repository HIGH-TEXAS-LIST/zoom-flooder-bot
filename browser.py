# -*- coding: utf-8 -*-

import logging
import os
import subprocess
import threading

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

try:
    from webdriver_manager.chrome import ChromeDriverManager

    _HAS_MANAGER = True
except ImportError:
    _HAS_MANAGER = False

log = logging.getLogger(__name__)

PAGE_LOAD_TIMEOUT = 30

# ── Cached singletons (resolved once, reused for every bot) ─────────────────
_options_cache = None
_driver_path_cache = None
_cache_lock = threading.Lock()


def _resolve_driver_path():
    """Resolve the ChromeDriver executable path once and cache it."""
    global _driver_path_cache
    if _driver_path_cache is not None:
        return _driver_path_cache

    with _cache_lock:
        if _driver_path_cache is not None:
            return _driver_path_cache

        if _HAS_MANAGER:
            try:
                _driver_path_cache = ChromeDriverManager().install()
                log.info("ChromeDriver resolved via webdriver-manager.")
                return _driver_path_cache
            except Exception as exc:
                log.warning(
                    "webdriver-manager failed (%s), falling back to local chromedriver.exe",
                    exc,
                )

        # Fallback: local chromedriver.exe
        _driver_path_cache = "chromedriver.exe"
        return _driver_path_cache


def get_chrome_options():
    """Return a cached Chrome Options instance (built once, reused)."""
    global _options_cache
    if _options_cache is not None:
        return _options_cache

    options = Options()

    # Logging suppression
    options.add_argument("--log-level=3")
    options.add_argument("--silent")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    # Stability
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--headless=new")  # modern headless (faster + more stable)

    # GPU / rendering — save memory
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-webgl")
    options.add_argument("--enable-unsafe-swiftshader")
    options.add_argument("--window-size=800,600")  # small but usable viewport
    options.add_argument("--blink-settings=imagesEnabled=false")  # skip image decoding

    # Audio / media
    options.add_argument("--mute-audio")
    options.add_argument("--disable-features=WebRtcHideLocalIpsWithMdns")

    # Permissions
    options.add_experimental_option(
        "prefs",
        {
            "profile.default_content_setting_values.media_stream_mic": 2,
            "profile.default_content_setting_values.media_stream_camera": 2,
            "profile.default_content_setting_values.notifications": 2,
        },
    )

    _options_cache = options
    return _options_cache


def create_driver():
    """Create and return a configured Chrome WebDriver instance."""
    options = get_chrome_options()
    service = Service(_resolve_driver_path())

    if os.name == "nt":
        service.creationflags = subprocess.CREATE_NO_WINDOW

    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver
