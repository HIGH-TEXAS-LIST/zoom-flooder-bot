# -*- coding: utf-8 -*-

import logging
import os
import random
import threading
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    TimeoutException,
    ElementClickInterceptedException,
    NoSuchFrameException,
)

from browser import create_driver

log = logging.getLogger(__name__)

# ── Screenshot directory ─────────────────────────────────────────────────
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def _take_screenshot(driver, bot_id, label=""):
    """Save a screenshot for debugging. Files go to ./screenshots/"""
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        filename = f"bot{bot_id + 1}_{label}_{ts}.png"
        filepath = os.path.join(SCREENSHOT_DIR, filename)
        driver.save_screenshot(filepath)
        log.info("Bot %d: Screenshot saved: %s", bot_id + 1, filename)
    except Exception as exc:
        log.debug("Bot %d: Screenshot failed: %s", bot_id + 1, exc)


ELEMENT_WAIT_TIMEOUT = 15
MAX_ATTEMPTS = 3
INPUT_SETTLE_DELAY = 0.25
POST_JOIN_DELAY = 1.0
JOIN_URL = "https://zoom.us/wc/join/{meeting_id}"

# Selectors Zoom has used across different versions of the web client
# Updated March 2026 — Zoom 7.0 web client + legacy fallbacks
_NAME_SELECTORS = [
    (By.ID, "input-for-name"),
    (By.ID, "inputname"),
    (By.CSS_SELECTOR, "input[data-testid='input-for-name']"),
    (By.CSS_SELECTOR, "input[aria-label*='your name' i]"),
    (By.CSS_SELECTOR, "input[aria-label*='name' i]"),
    (By.CSS_SELECTOR, "input[name='name']"),
    (By.CSS_SELECTOR, "input[placeholder*='your name' i]"),
    (By.CSS_SELECTOR, "input[placeholder*='name' i]"),
]
_PWD_SELECTORS = [
    (By.ID, "input-for-pwd"),
    (By.ID, "inputpasscode"),
    (By.CSS_SELECTOR, "input[data-testid='input-for-pwd']"),
    (By.CSS_SELECTOR, "input[aria-label*='meeting passcode' i]"),
    (By.CSS_SELECTOR, "input[aria-label*='passcode' i]"),
    (By.CSS_SELECTOR, "input[name='password']"),
    (By.CSS_SELECTOR, "input[placeholder*='passcode' i]"),
    (By.CSS_SELECTOR, "input[placeholder*='password' i]"),
    (By.CSS_SELECTOR, "input[type='password']"),
]
_JOIN_SELECTORS = [
    (By.CSS_SELECTOR, "button.preview-join-button"),
    (By.XPATH, "//button[contains(@class, 'preview-join-button')]"),
    (By.CSS_SELECTOR, "button[data-testid='join-btn']"),
    (By.XPATH, "//button[contains(@class, 'join-btn')]"),
    (By.CSS_SELECTOR, "button.btn-join"),
    (By.CSS_SELECTOR, "#joinBtn"),
    (By.XPATH, "//button[contains(text(), 'Join')]"),
    (By.XPATH, "//button[contains(text(), 'join')]"),
]

# ── Thread-safe unique-name pool ────────────────────────────────────────────
_name_pool = []
_name_lock = threading.Lock()


def init_name_pool(names_list):
    """Shuffle the names list once so each bot gets a unique name."""
    global _name_pool
    _name_pool = list(names_list)
    random.shuffle(_name_pool)


def _pick_unique_name():
    """Pop a unique name from the pool; fall back to random suffix if empty."""
    with _name_lock:
        if _name_pool:
            return _name_pool.pop()
    return f"User_{random.randint(1000, 9999)}"


# ── Error detection XPaths / selectors ──────────────────────────────────────
_ERROR_SELECTORS = [
    (By.XPATH, "//*[contains(text(), 'meeting password is wrong')]"),
    (By.XPATH, "//*[contains(text(), 'meeting passcode is wrong')]"),
    (By.XPATH, "//*[contains(text(), 'This meeting has been ended')]"),
    (By.XPATH, "//*[contains(text(), 'meeting ID is not valid')]"),
    (By.XPATH, "//*[contains(text(), 'meeting link is invalid')]"),
    (By.XPATH, "//*[contains(text(), 'Unable to join')]"),
    (By.XPATH, "//*[contains(text(), 'The meeting has not started')]"),
    (By.XPATH, "//*[contains(text(), \"can't join this call\")]"),
    (By.XPATH, "//*[contains(text(), 'You have been removed')]"),
    (By.XPATH, "//*[contains(text(), 'meeting has ended')]"),
    (By.CSS_SELECTOR, ".error-message"),
]

# ── Waiting room detection ─────────────────────────────────────────────────
_WAITING_ROOM_SELECTORS = [
    (By.XPATH, "//*[contains(text(), 'host will admit you')]"),
    (By.XPATH, "//*[contains(text(), 'Waiting for the host')]"),
    (By.XPATH, "//*[contains(text(), 'Host has joined')]"),
    (By.XPATH, "//*[contains(text(), 'will let you in soon')]"),
    (By.XPATH, "//*[contains(text(), \"Please wait, the meeting host\")]"),
]


def _check_join_errors(driver):
    """Return an error message if the page shows a Zoom error banner, else None."""
    for by, selector in _ERROR_SELECTORS:
        elems = driver.find_elements(by, selector)
        if elems:
            return elems[0].text
    return None


# ── Element helpers ─────────────────────────────────────────────────────────
def _find_element_multi(driver, selectors):
    """Try multiple selectors and return the first visible element, or None."""
    for by, sel in selectors:
        try:
            el = driver.find_element(by, sel)
            if el.is_displayed():
                return el
        except Exception:
            continue
    return None


def _find_element_js(driver, role):
    """JS-based fallback to find name/password/join elements in React shadow DOM.

    *role* is one of 'name', 'password', 'join'.
    """
    JS = """
    var role = arguments[0];
    if (role === 'name') {
        // Find visible text input that looks like a name field
        var inputs = document.querySelectorAll('input[type="text"], input:not([type])');
        for (var i = 0; i < inputs.length; i++) {
            var el = inputs[i];
            if (el.offsetWidth === 0) continue;
            var ph = (el.placeholder || '').toLowerCase();
            var al = (el.getAttribute('aria-label') || '').toLowerCase();
            var id = (el.id || '').toLowerCase();
            if (ph.indexOf('name') !== -1 || al.indexOf('name') !== -1 ||
                id.indexOf('name') !== -1) return el;
        }
    } else if (role === 'password') {
        var inputs = document.querySelectorAll('input[type="password"], input');
        for (var i = 0; i < inputs.length; i++) {
            var el = inputs[i];
            if (el.offsetWidth === 0) continue;
            var ph = (el.placeholder || '').toLowerCase();
            var al = (el.getAttribute('aria-label') || '').toLowerCase();
            var id = (el.id || '').toLowerCase();
            var tp = (el.type || '').toLowerCase();
            if (tp === 'password' || ph.indexOf('passcode') !== -1 ||
                ph.indexOf('password') !== -1 || al.indexOf('passcode') !== -1 ||
                id.indexOf('pwd') !== -1 || id.indexOf('passcode') !== -1) return el;
        }
    } else if (role === 'join') {
        var buttons = document.querySelectorAll('button');
        for (var i = 0; i < buttons.length; i++) {
            var el = buttons[i];
            if (el.offsetWidth === 0) continue;
            var text = (el.textContent || '').trim().toLowerCase();
            var cls = (el.className || '').toLowerCase();
            if ((text === 'join' || text.indexOf('join') === 0) &&
                text.length < 20) return el;
            if (cls.indexOf('join') !== -1 || cls.indexOf('preview-join') !== -1) return el;
        }
    }
    return null;
    """
    try:
        return driver.execute_script(JS, role)
    except Exception:
        return None


def _has_join_form(driver):
    """Return True if name field is present (password may appear on step 2)."""
    if _find_element_multi(driver, _NAME_SELECTORS) is not None:
        return True
    # JS fallback for React-rendered inputs
    return _find_element_js(driver, 'name') is not None


def _switch_to_zoom_content(driver, bot_id):
    """Try to switch into an iframe that contains the join form.

    Zoom's web client sometimes nests the form inside one or more iframes.
    Tries the main document first, then each iframe recursively (one level).
    Returns True if BOTH name and password inputs were found.
    """
    # Check main document first
    if _has_join_form(driver):
        log.info("Bot %d: Form found in main document.", bot_id + 1)
        return True

    # Try each iframe
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    log.debug("Bot %d: Found %d iframes, checking each…", bot_id + 1, len(iframes))

    for idx, iframe in enumerate(iframes):
        try:
            driver.switch_to.frame(iframe)
            if _has_join_form(driver):
                log.info("Bot %d: Form found in iframe #%d.", bot_id + 1, idx)
                return True
            # Check nested iframes (one level deep)
            nested = driver.find_elements(By.TAG_NAME, "iframe")
            for nidx, nested_frame in enumerate(nested):
                try:
                    driver.switch_to.frame(nested_frame)
                    if _has_join_form(driver):
                        log.info(
                            "Bot %d: Form found in nested iframe #%d.%d.",
                            bot_id + 1, idx, nidx,
                        )
                        return True
                    driver.switch_to.parent_frame()
                except (NoSuchFrameException, Exception):
                    try:
                        driver.switch_to.parent_frame()
                    except Exception:
                        pass
            driver.switch_to.default_content()
        except (NoSuchFrameException, Exception):
            try:
                driver.switch_to.default_content()
            except Exception:
                pass

    return False


def _debug_dump(driver, bot_id):
    """Log page state for debugging when form can't be found."""
    page_title = driver.title
    page_url = driver.current_url
    log.info(
        "Bot %d: Form not visible (title=%r, url=%s).",
        bot_id + 1, page_title, page_url,
    )
    # Switch back to main doc for full dump
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    # Count iframes
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    log.info("Bot %d: Page has %d iframes in main document.", bot_id + 1, len(iframes))
    for i, fr in enumerate(iframes):
        log.info(
            "Bot %d: iframe #%d src=%r id=%r",
            bot_id + 1, i,
            (fr.get_attribute("src") or "")[:120],
            fr.get_attribute("id"),
        )

    # Log inputs/buttons from ALL frames
    frames_to_check = [None] + list(range(len(iframes)))
    for fidx in frames_to_check:
        try:
            if fidx is None:
                driver.switch_to.default_content()
                ctx = "main"
            else:
                driver.switch_to.default_content()
                driver.switch_to.frame(iframes[fidx])
                ctx = f"iframe#{fidx}"
            inputs = driver.find_elements(By.TAG_NAME, "input")
            buttons = driver.find_elements(By.TAG_NAME, "button")
            if inputs or buttons:
                for j, el in enumerate(inputs):
                    log.info(
                        "Bot %d [%s]: <input #%d> id=%r name=%r type=%r placeholder=%r",
                        bot_id + 1, ctx, j,
                        el.get_attribute("id"),
                        el.get_attribute("name"),
                        el.get_attribute("type"),
                        el.get_attribute("placeholder"),
                    )
                for j, el in enumerate(buttons):
                    log.info(
                        "Bot %d [%s]: <button #%d> id=%r text=%r",
                        bot_id + 1, ctx, j,
                        el.get_attribute("id"),
                        el.text[:80] if el.text else "",
                    )
        except Exception:
            pass

    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    # Save screenshot
    try:
        shot_dir = os.path.dirname(os.path.abspath(__file__))
        shot_path = os.path.join(shot_dir, f"debug_bot{bot_id + 1}.png")
        driver.save_screenshot(shot_path)
        log.info("Bot %d: Screenshot saved to %s", bot_id + 1, shot_path)
    except Exception:
        pass


def _verify_input_fields(driver, bot_name, passcode):
    """Re-check that name and passcode fields have values, refill if empty."""
    try:
        name_el = _find_element_multi(driver, _NAME_SELECTORS)
        pwd_el = _find_element_multi(driver, _PWD_SELECTORS)
        if not name_el or not pwd_el:
            return False

        if not name_el.get_attribute("value"):
            name_el.clear()
            name_el.send_keys(bot_name)
            time.sleep(INPUT_SETTLE_DELAY)

        if not pwd_el.get_attribute("value"):
            pwd_el.clear()
            pwd_el.send_keys(passcode)
            time.sleep(INPUT_SETTLE_DELAY)

        return bool(
            name_el.get_attribute("value") and pwd_el.get_attribute("value")
        )
    except Exception as exc:
        log.debug("Field verification error: %s", exc)
        return False


def _dismiss_gates(driver, bot_id):
    """Click through cookie banners, disclaimers, and other pre-join dialogs."""
    # 0. Try to bypass cookies via JS (set OneTrust consent cookies directly)
    try:
        driver.execute_script("""
            document.cookie = 'OptanonAlertBoxClosed=' + new Date().toISOString() + ';path=/;domain=.zoom.us';
            document.cookie = 'OptanonConsent=isGpcEnabled=0&datestamp=' + encodeURIComponent(new Date().toISOString()) + '&version=6&isIABGlobal=false&hosts=&landingPath=NotLandingPage&groups=C0001%3A1%2CC0002%3A1%2CC0003%3A1%2CC0004%3A1;path=/;domain=.zoom.us';
            var banner = document.getElementById('onetrust-banner-sdk');
            if (banner) banner.style.display = 'none';
        """)
    except Exception:
        pass

    # 1. Accept cookies (OneTrust banner) — fallback if JS bypass didn't hide it
    _COOKIE_ACCEPT = [
        (By.ID, "onetrust-accept-btn-handler"),
        (By.CSS_SELECTOR, "#onetrust-accept-btn-handler"),
        (By.CSS_SELECTOR, ".onetrust-close-btn-handler"),
        (By.XPATH, "//button[contains(text(), 'Accept All')]"),
        (By.XPATH, "//button[contains(text(), 'Accept Cookies')]"),
        (By.XPATH, "//button[contains(@id, 'accept')]"),
    ]
    for by, sel in _COOKIE_ACCEPT:
        try:
            btn = driver.find_element(by, sel)
            if btn.is_displayed():
                btn.click()
                log.info("Bot %d: Accepted cookies.", bot_id + 1)
                time.sleep(1)
                break
        except Exception:
            continue

    # 2. Accept Zoom disclaimer / terms of service (use JS click — may be behind overlay)
    try:
        btn = driver.find_element(By.ID, "disclaimer_agree")
        driver.execute_script("arguments[0].click();", btn)
        log.info("Bot %d: Accepted disclaimer.", bot_id + 1)
        time.sleep(2)
    except Exception:
        # Fallback: try text-based selectors
        for by, sel in [
            (By.XPATH, "//button[contains(text(), 'Agree')]"),
            (By.XPATH, "//button[contains(text(), 'Accept')]"),
            (By.XPATH, "//button[contains(text(), 'I Agree')]"),
        ]:
            try:
                btn = driver.find_element(by, sel)
                driver.execute_script("arguments[0].click();", btn)
                log.info("Bot %d: Accepted disclaimer (fallback).", bot_id + 1)
                time.sleep(2)
                break
            except Exception:
                continue

    # 3. Handle "Continue" button (audio/video prompt)
    for by, sel in [
        (By.CLASS_NAME, "continue"),
        (By.XPATH, "//button[contains(text(), 'Join Audio by Computer')]"),
        (By.XPATH, "//button[contains(text(), 'Computer Audio')]"),
        (By.XPATH, "//span[contains(text(), 'Computer Audio')]"),
        (By.CSS_SELECTOR, "button[aria-label*='Join Audio' i]"),
    ]:
        try:
            btn = driver.find_element(by, sel)
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                log.info("Bot %d: Clicked continue/audio.", bot_id + 1)
                time.sleep(1)
                break
        except Exception:
            continue

    # 4. Dismiss recording notice ("Got it" button)
    for by, sel in [
        (By.XPATH, "//button[contains(text(), 'Got it')]"),
        (By.XPATH, "//button[contains(text(), 'got it')]"),
        (By.XPATH, "//button[contains(text(), 'OK')]"),
    ]:
        try:
            btn = driver.find_element(by, sel)
            if btn.is_displayed():
                driver.execute_script("arguments[0].click();", btn)
                log.info("Bot %d: Dismissed recording notice.", bot_id + 1)
                time.sleep(1)
                break
        except Exception:
            continue


# ── Main bot launcher ───────────────────────────────────────────────────────
def launch_bot(bot_id, meeting_id, passcode, names_list, custom_name="",
               stop_event=None, proxies=None, chat_recipient="", chat_message=""):
    """Launch a single bot that joins the given Zoom meeting.

    Returns (driver, elapsed_seconds) on success or (None, elapsed_seconds) on failure.
    """
    driver = None
    t_start = time.monotonic()

    def _stopped():
        return stop_event is not None and stop_event.is_set()

    for attempt in range(MAX_ATTEMPTS):
        if _stopped():
            log.info("Bot %d: Cancelled before attempt %d.", bot_id + 1, attempt + 1)
            elapsed = time.monotonic() - t_start
            return (None, elapsed)

        try:
            # Pick a random proxy for this attempt
            proxy = random.choice(proxies) if proxies else None
            if proxy:
                log.info("Bot %d: Using proxy %s", bot_id + 1,
                         proxy.split("@")[-1] if "@" in proxy else proxy)
            driver = create_driver(proxy=proxy)
            wait = WebDriverWait(driver, ELEMENT_WAIT_TIMEOUT)

            driver.get(JOIN_URL.format(meeting_id=meeting_id))

            bot_name = custom_name or _pick_unique_name()

            # Wait for page to fully load (Zoom SPA needs time)
            if _stopped():
                driver.quit(); driver = None
                return (None, time.monotonic() - t_start)
            time.sleep(5)

            # ── Dismiss pre-join gates (cookies, disclaimer, etc.) ──────
            driver.switch_to.default_content()
            _dismiss_gates(driver, bot_id)

            # Wait for the web client to load after dismissing gates
            # Poll for form with increasing waits (SPA may need time to render)
            form_found = False
            for wait_step in range(6):
                if _stopped():
                    driver.quit(); driver = None
                    return (None, time.monotonic() - t_start)
                time.sleep(2)
                driver.switch_to.default_content()
                if _switch_to_zoom_content(driver, bot_id):
                    form_found = True
                    break

            if not form_found:
                # Dump diagnostics on first failure only
                if attempt == 0:
                    _debug_dump(driver, bot_id)
                else:
                    log.info("Bot %d: Form not visible, retrying…", bot_id + 1)
                driver.quit()
                driver = None
                time.sleep(3)
                continue

            # ── Step 1: Fill name ────────────────────────────────────
            name_el = _find_element_multi(driver, _NAME_SELECTORS)
            if not name_el:
                name_el = _find_element_js(driver, 'name')
            if not name_el:
                log.warning("Bot %d: Name field missing.", bot_id + 1)
                driver.quit()
                driver = None
                time.sleep(2)
                continue

            # Clear and fill with JS fallback for React controlled inputs
            try:
                driver.execute_script(
                    "var el = arguments[0]; el.focus(); el.value = ''; "
                    "var nev = new Event('input', {bubbles:true}); el.dispatchEvent(nev);",
                    name_el,
                )
            except Exception:
                pass
            name_el.clear()
            name_el.send_keys(bot_name)
            # Trigger React onChange via native input event
            try:
                driver.execute_script(
                    "var ev = new Event('input', {bubbles:true}); arguments[0].dispatchEvent(ev);",
                    name_el,
                )
            except Exception:
                pass
            time.sleep(INPUT_SETTLE_DELAY)
            log.info("Bot %d: Filled name '%s'.", bot_id + 1, bot_name)

            # Check if passcode field is on the same page (old-style single-step)
            pwd_el = _find_element_multi(driver, _PWD_SELECTORS) or _find_element_js(driver, 'password')
            if pwd_el:
                pwd_el.clear()
                pwd_el.send_keys(passcode)
                time.sleep(INPUT_SETTLE_DELAY)
                log.info("Bot %d: Filled passcode (single-step).", bot_id + 1)

            # Click Join
            _click_join(driver, bot_id, bot_name, passcode)

            # ── Step 2: Handle passcode on second page (if needed) ──
            if not pwd_el:
                time.sleep(3)
                # Re-check frames after page transition
                driver.switch_to.default_content()
                _switch_to_zoom_content(driver, bot_id)

                pwd_el = None
                for _ in range(5):
                    pwd_el = _find_element_multi(driver, _PWD_SELECTORS) or _find_element_js(driver, 'password')
                    if pwd_el:
                        break
                    time.sleep(2)

                if pwd_el:
                    pwd_el.clear()
                    pwd_el.send_keys(passcode)
                    time.sleep(INPUT_SETTLE_DELAY)
                    log.info("Bot %d: Filled passcode (step 2).", bot_id + 1)
                    # Click Join again for passcode submission
                    _click_join(driver, bot_id, bot_name, passcode)
                else:
                    log.info("Bot %d: No passcode requested, continuing…", bot_id + 1)

            time.sleep(POST_JOIN_DELAY)

            # ── Verify join actually succeeded ──────────────────────────
            error_msg = _check_join_errors(driver)
            if error_msg:
                log.warning("Bot %d: Zoom error after join: %s", bot_id + 1, error_msg)
                driver.quit()
                driver = None
                elapsed = time.monotonic() - t_start
                return (None, elapsed)

            # ── Check for waiting room ──────────────────────────────────
            in_waiting_room = _find_element_multi(driver, _WAITING_ROOM_SELECTORS)
            if in_waiting_room:
                log.info("Bot %d: In waiting room, waiting for host admission…", bot_id + 1)
                # Wait up to 60s for host to admit
                for _ in range(30):
                    if _stopped():
                        driver.quit(); driver = None
                        return (None, time.monotonic() - t_start)
                    time.sleep(2)
                    if not _find_element_multi(driver, _WAITING_ROOM_SELECTORS):
                        log.info("Bot %d: Admitted from waiting room.", bot_id + 1)
                        break
                else:
                    log.warning("Bot %d: Timed out in waiting room.", bot_id + 1)

            # ── Dismiss post-join gates (audio prompt, recording notice) ──
            driver.switch_to.default_content()
            _dismiss_gates(driver, bot_id)

            elapsed = time.monotonic() - t_start
            log.info("Bot %d joined! (%.1fs)", bot_id + 1, elapsed)
            _take_screenshot(driver, bot_id, "joined")

            # ── Send chat message if configured ─────────────────────
            if chat_message and driver:
                send_chat_message(driver, bot_id, chat_message, chat_recipient)
                # Auto-leave after sending message
                time.sleep(1)
                log.info("Bot %d: Auto-leaving after chat message.", bot_id + 1)
                leave_meeting(driver, bot_id + 1)
                time.sleep(1)
                try:
                    driver.quit()
                except Exception:
                    pass
                return ("left", elapsed)  # Signal: succeeded + already left

            return (driver, elapsed)

        except Exception as exc:
            if attempt < MAX_ATTEMPTS - 1:
                log.warning(
                    "Bot %d: Attempt %d/%d failed: %s",
                    bot_id + 1, attempt + 1, MAX_ATTEMPTS, exc,
                )
            else:
                log.error(
                    "Bot %d: Failed after %d attempts: %s",
                    bot_id + 1, MAX_ATTEMPTS, exc,
                )

            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None

            time.sleep(2 * (attempt + 1))

    elapsed = time.monotonic() - t_start
    return (None, elapsed)


# ── Chat message ──────────────────────────────────────────────────────────
_CHAT_BTN_SELECTORS = [
    (By.CSS_SELECTOR, "button[aria-label*='open the chat pane' i]"),
    (By.CSS_SELECTOR, "button[aria-label*='chat' i]"),
    (By.CSS_SELECTOR, "button[data-testid='chat-button']"),
    (By.XPATH, "//button[contains(@class, 'footer-button__chat')]"),
    (By.XPATH, "//button[contains(@class, 'footer-button')][.//span[contains(text(), 'Chat')]]"),
    (By.XPATH, "//button[contains(text(), 'Chat')]"),
    (By.CSS_SELECTOR, "button.footer__chat-btn"),
]
_CHAT_INPUT_SELECTORS = [
    (By.CSS_SELECTOR, "textarea[aria-label*='chat message' i]"),
    (By.CSS_SELECTOR, "textarea[aria-label*='type message' i]"),
    (By.CSS_SELECTOR, "textarea[aria-label*='chat' i]"),
    (By.CSS_SELECTOR, "textarea[aria-label*='message' i]"),
    (By.CSS_SELECTOR, "textarea[placeholder*='Type message' i]"),
    (By.CSS_SELECTOR, "textarea[placeholder*='message' i]"),
    (By.CSS_SELECTOR, "div[contenteditable='true'][aria-label*='chat' i]"),
    (By.CSS_SELECTOR, "div[contenteditable='true'][aria-label*='message' i]"),
    (By.CSS_SELECTOR, "div[contenteditable='true'][data-placeholder*='message' i]"),
    (By.CSS_SELECTOR, "div[contenteditable='true'][data-placeholder*='Type' i]"),
    (By.CSS_SELECTOR, ".chat-box__chat-textarea textarea"),
    (By.CSS_SELECTOR, "#wc-container-right textarea"),
    (By.CSS_SELECTOR, "[class*='chat'] textarea"),
    (By.CSS_SELECTOR, "[class*='chat'] [contenteditable='true']"),
    (By.XPATH, "//textarea[contains(@placeholder, 'Type message')]"),
    (By.XPATH, "//textarea[contains(@placeholder, 'type message')]"),
]
_CHAT_SEND_SELECTORS = [
    (By.CSS_SELECTOR, "button[aria-label*='send message' i]"),
    (By.CSS_SELECTOR, "button[aria-label*='send' i]"),
    (By.CSS_SELECTOR, "button[data-testid='send-chat-btn']"),
    (By.CSS_SELECTOR, "button.chat-box__send-btn"),
    (By.XPATH, "//button[contains(@class, 'send')]"),
]

# Selectors for the chat "To" recipient dropdown
_CHAT_RECEIVER_SELECTORS = [
    (By.CSS_SELECTOR, "button[aria-label*='send to' i]"),
    (By.CSS_SELECTOR, "button[aria-label*='receiver' i]"),
    (By.CSS_SELECTOR, "button[aria-label*='chat receiver' i]"),
    (By.CSS_SELECTOR, "button[data-testid='chat-receiver-btn']"),
    (By.CSS_SELECTOR, ".chat-receiver-list__receiver"),
    (By.CSS_SELECTOR, "[class*='chat-receiver']"),
    (By.CSS_SELECTOR, "button[class*='receiver']"),
    (By.XPATH, "//button[contains(@class, 'dropdown')]//span[contains(text(), 'Everyone')]"),
    (By.XPATH, "//button[.//span[contains(text(), 'Everyone')]]"),
    (By.XPATH, "//button[contains(text(), 'Everyone')]"),
    (By.CSS_SELECTOR, "a[aria-haspopup='true'][class*='chat']"),
    (By.CSS_SELECTOR, "div[class*='chat-to'] button"),
    (By.CSS_SELECTOR, "[class*='chat'] [role='combobox']"),
    (By.CSS_SELECTOR, "[class*='chat'] select"),
]


def _xpath_escape(value):
    """Safely escape a string for use in XPath expressions.

    Handles strings containing single quotes, double quotes, or both.
    """
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    # Contains both quote types — use concat()
    parts = value.split("'")
    return "concat(" + ",\"'\",".join(f"'{p}'" for p in parts) + ")"


def _css_escape(value):
    """Escape a string for safe use in CSS attribute selectors."""
    return value.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').replace("]", "\\]")


def _select_chat_recipient(driver, bot_id, recipient_name):
    """Select a specific user from the chat recipient dropdown.

    Returns True if the recipient was selected, False otherwise.
    """
    try:
        # Click the receiver/To dropdown
        receiver_btn = _find_element_multi(driver, _CHAT_RECEIVER_SELECTORS)
        if not receiver_btn:
            # Debug: log what buttons exist near the chat area
            try:
                buttons = driver.find_elements(By.CSS_SELECTOR, "button")
                chat_buttons = [b for b in buttons if any(
                    kw in (b.get_attribute("aria-label") or "").lower() + (b.text or "").lower()
                    for kw in ("send to", "everyone", "receiver")
                )]
                if chat_buttons:
                    labels = [f"'{b.get_attribute('aria-label') or b.text}'" for b in chat_buttons[:5]]
                    log.info("Bot %d: Nearby chat buttons: %s", bot_id + 1, ", ".join(labels))
                    receiver_btn = chat_buttons[0]
                else:
                    log.warning("Bot %d: Chat recipient dropdown not found (no matching buttons).", bot_id + 1)
                    return False
            except Exception:
                log.warning("Bot %d: Chat recipient dropdown not found.", bot_id + 1)
                return False

        log.info("Bot %d: Clicking recipient dropdown: '%s'",
                 bot_id + 1, receiver_btn.get_attribute("aria-label") or receiver_btn.text or "(no label)")
        driver.execute_script("arguments[0].click();", receiver_btn)
        time.sleep(2)
        _take_screenshot(driver, bot_id, "dropdown_opened")

        recipient_lower = recipient_name.lower().strip()
        name_parts = [p.lower() for p in recipient_name.split() if len(p) > 2]

        # Use JavaScript to find dropdown items — Zoom renders them in a scroll container
        # that Selenium's normal element search sometimes misses
        JS_FIND_DROPDOWN = """
        // Strategy 1: Find the dropdown/popover container that appeared after click
        // Look for elements with role=listbox, role=menu, or class containing 'dropdown'/'popover'
        var results = [];
        var containers = document.querySelectorAll(
            '[role="listbox"], [role="menu"], [role="dialog"], ' +
            '[class*="dropdown"], [class*="popover"], [class*="receiver"], ' +
            '[class*="chat-receiver"], [class*="select"], [class*="popup"]'
        );

        // Also check for any scrollable container that appeared in the chat area
        if (containers.length === 0) {
            containers = document.querySelectorAll('[style*="overflow"], [class*="scroll"], [class*="list"]');
        }

        for (var c = 0; c < containers.length; c++) {
            var container = containers[c];
            var rect = container.getBoundingClientRect();
            if (rect.width < 80 || rect.height < 30) continue;
            var style = window.getComputedStyle(container);
            if (style.display === 'none' || style.visibility === 'hidden') continue;

            // Get all child text elements
            var children = container.querySelectorAll('*');
            for (var i = 0; i < children.length; i++) {
                var el = children[i];
                var elRect = el.getBoundingClientRect();
                if (elRect.width < 30 || elRect.height < 10) continue;
                // Get direct text (not deeply nested text)
                var text = '';
                for (var n = 0; n < el.childNodes.length; n++) {
                    if (el.childNodes[n].nodeType === 3) text += el.childNodes[n].textContent;
                }
                text = text.trim();
                if (!text) text = (el.textContent || '').trim();
                if (!text || text.length > 80 || text.length < 3) continue;
                results.push({text: text, tag: el.tagName, idx: Array.from(document.querySelectorAll('*')).indexOf(el)});
            }
        }

        // Strategy 2: If nothing found, do a broader scan for elements near the "Everyone" text
        if (results.length === 0) {
            var allEls = document.querySelectorAll('*');
            var everyoneEl = null;
            for (var i = 0; i < allEls.length; i++) {
                if ((allEls[i].textContent || '').trim() === 'Everyone' && allEls[i].offsetHeight > 0) {
                    everyoneEl = allEls[i];
                    break;
                }
            }
            if (everyoneEl) {
                // Get the parent container and scan siblings
                var parent = everyoneEl.parentElement;
                while (parent && parent.children.length < 3) parent = parent.parentElement;
                if (parent) {
                    var siblings = parent.querySelectorAll('*');
                    for (var i = 0; i < siblings.length; i++) {
                        var el = siblings[i];
                        var text = (el.textContent || '').trim().split('\\n')[0].trim();
                        if (!text || text.length > 80 || text.length < 3) continue;
                        if (el.children.length > 2) continue;
                        results.push({text: text, tag: el.tagName, idx: Array.from(document.querySelectorAll('*')).indexOf(el)});
                    }
                }
            }
        }

        return results;
        """

        try:
            all_items = driver.execute_script(JS_FIND_DROPDOWN)
            # Filter to likely dropdown items (exclude toolbar text)
            toolbar_noise = {'audio', 'video', 'participants', 'more', 'leave',
                             'raise hand', 'reactions', 'share screen', 'chat',
                             'meeting chat', 'type message here ...', 'ok', 'new',
                             'floating reactions', 'who can see your messages?'}
            dropdown_names = []
            for item in all_items:
                text = item['text'].strip()
                text_clean = text.split('\n')[0].strip()  # First line only
                if text_clean.lower() in toolbar_noise:
                    continue
                if len(text_clean) < 3:
                    continue
                dropdown_names.append(item)

            # Log what we found
            found_names = [d['text'].split('\n')[0].strip() for d in dropdown_names[:20]]
            log.info("Bot %d: Dropdown items found via JS: %s", bot_id + 1, found_names)

            # Search for recipient
            matched_idx = None
            for item in dropdown_names:
                item_text = item['text'].lower()
                item_first = item['text'].split('\n')[0].strip().lower()
                # Exact or contains match
                if recipient_lower in item_first:
                    matched_idx = item['idx']
                    log.info("Bot %d: Matched recipient '%s' -> '%s'",
                             bot_id + 1, recipient_name, item['text'].split(chr(10))[0].strip())
                    break
                # Partial name match
                if any(part in item_first for part in name_parts):
                    if item_first not in toolbar_noise:
                        matched_idx = item['idx']
                        log.info("Bot %d: Partial name match '%s' -> '%s'",
                                 bot_id + 1, recipient_name, item['text'].split(chr(10))[0].strip())
                        break

            if matched_idx is not None:
                # Click the matched element via JS index
                driver.execute_script(f"""
                    var allEls = document.querySelectorAll('*');
                    allEls[{matched_idx}].click();
                """)
                log.info("Bot %d: Selected chat recipient '%s'.", bot_id + 1, recipient_name)
                time.sleep(1)
                _take_screenshot(driver, bot_id, "recipient_selected")
                return True

            # Not found — might need to scroll the dropdown
            # Try scrolling the dropdown container and searching again
            JS_SCROLL_AND_FIND = """
            var target = arguments[0].toLowerCase();
            var containers = document.querySelectorAll('[class*="scroll"], [class*="list"], [style*="overflow"]');
            for (var c = 0; c < containers.length; c++) {
                var container = containers[c];
                var rect = container.getBoundingClientRect();
                if (rect.height < 50 || rect.height > 500) continue;
                if (rect.width < 100) continue;
                // Scroll down in increments
                var maxScroll = container.scrollHeight;
                for (var pos = 0; pos < maxScroll; pos += 200) {
                    container.scrollTop = pos;
                    // Brief pause handled by checking after
                }
                // Now check all children
                var items = container.querySelectorAll('*');
                for (var i = 0; i < items.length; i++) {
                    var text = (items[i].textContent || '').trim().toLowerCase();
                    if (text.length > 80 || text.length < 2) continue;
                    if (text.indexOf(target) !== -1) {
                        items[i].scrollIntoView({block: 'center'});
                        return items[i];
                    }
                }
            }
            return null;
            """

            log.info("Bot %d: Scrolling dropdown to find '%s'...", bot_id + 1, recipient_name)
            found_el = driver.execute_script(JS_SCROLL_AND_FIND, recipient_lower)
            if found_el:
                time.sleep(0.5)
                _take_screenshot(driver, bot_id, "recipient_scrolled")
                driver.execute_script("arguments[0].click();", found_el)
                log.info("Bot %d: Selected chat recipient '%s' (after scroll).", bot_id + 1, recipient_name)
                time.sleep(1)
                return True

            # Also try partial name parts
            for part in name_parts:
                found_el = driver.execute_script(JS_SCROLL_AND_FIND, part)
                if found_el:
                    time.sleep(0.5)
                    driver.execute_script("arguments[0].click();", found_el)
                    log.info("Bot %d: Selected recipient via partial match '%s'.", bot_id + 1, part)
                    time.sleep(1)
                    return True

        except Exception as js_exc:
            log.warning("Bot %d: JS dropdown search error: %s", bot_id + 1, js_exc)

        log.warning("Bot %d: Recipient '%s' not found in chat dropdown.", bot_id + 1, recipient_name)
        _take_screenshot(driver, bot_id, "recipient_final_fail")
        # Close dropdown by clicking the receiver button again or pressing Escape
        try:
            driver.execute_script("arguments[0].click();", receiver_btn)
            time.sleep(0.5)
        except Exception:
            try:
                driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                time.sleep(0.5)
            except Exception:
                pass
        return False

    except Exception as exc:
        log.warning("Bot %d: Failed to select recipient '%s': %s", bot_id + 1, recipient_name, exc)
        return False


def send_chat_message(driver, bot_id, message, recipient=""):
    """Open the chat panel and send a message after joining.

    If *recipient* is provided, selects that user from the "To" dropdown
    to send a direct message instead of messaging Everyone.

    Returns True if the message was sent, False otherwise.
    """
    try:
        driver.switch_to.default_content()

        # Switch into Zoom iframe if needed
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for iframe in iframes:
            try:
                driver.switch_to.frame(iframe)
                if _find_element_multi(driver, _CHAT_BTN_SELECTORS):
                    break
                driver.switch_to.default_content()
            except Exception:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

        # Step 1: Open chat panel
        chat_btn = _find_element_multi(driver, _CHAT_BTN_SELECTORS)
        if chat_btn:
            driver.execute_script("arguments[0].click();", chat_btn)
            log.info("Bot %d: Opened chat panel.", bot_id + 1)
            time.sleep(1.5)
        else:
            log.info("Bot %d: Chat button not found, trying input directly.", bot_id + 1)

        # Step 1.5: Select specific recipient if provided
        if recipient:
            _take_screenshot(driver, bot_id, "before_recipient_select")
            if not _select_chat_recipient(driver, bot_id, recipient):
                log.warning("Bot %d: Could not select recipient '%s', sending to Everyone instead.", bot_id + 1, recipient)
                _take_screenshot(driver, bot_id, "recipient_not_found")

        # Step 2: Find the chat input (re-locate iframe if needed after dropdown interaction)
        chat_input = None
        for attempt in range(5):
            chat_input = _find_element_multi(driver, _CHAT_INPUT_SELECTORS)
            if chat_input:
                break
            # Try switching frames — dropdown interaction may have changed focus
            if attempt == 1:
                driver.switch_to.default_content()
                iframes = driver.find_elements(By.TAG_NAME, "iframe")
                for iframe in iframes:
                    try:
                        driver.switch_to.frame(iframe)
                        chat_input = _find_element_multi(driver, _CHAT_INPUT_SELECTORS)
                        if chat_input:
                            break
                        driver.switch_to.default_content()
                    except Exception:
                        try:
                            driver.switch_to.default_content()
                        except Exception:
                            pass
                if chat_input:
                    break
            time.sleep(1)

        if not chat_input:
            # JS fallback: find any textarea or contenteditable with chat-like placeholder
            try:
                chat_input = driver.execute_script("""
                    // Try textarea first
                    var tas = document.querySelectorAll('textarea');
                    for (var i = 0; i < tas.length; i++) {
                        var ph = (tas[i].placeholder || '').toLowerCase();
                        if (ph.indexOf('message') !== -1 || ph.indexOf('type') !== -1) return tas[i];
                    }
                    // Try contenteditable
                    var ces = document.querySelectorAll('[contenteditable="true"]');
                    for (var i = 0; i < ces.length; i++) {
                        var dp = (ces[i].getAttribute('data-placeholder') || '').toLowerCase();
                        var al = (ces[i].getAttribute('aria-label') || '').toLowerCase();
                        if (dp.indexOf('message') !== -1 || al.indexOf('chat') !== -1) return ces[i];
                    }
                    // Last resort: any visible textarea
                    for (var i = 0; i < tas.length; i++) {
                        if (tas[i].offsetWidth > 0 && tas[i].offsetHeight > 0) return tas[i];
                    }
                    return null;
                """)
                if chat_input:
                    log.info("Bot %d: Found chat input via JS fallback.", bot_id + 1)
            except Exception:
                pass

        if not chat_input:
            log.warning("Bot %d: Chat input not found, cannot send message.", bot_id + 1)
            _take_screenshot(driver, bot_id, "chat_input_not_found")
            return False

        # Step 3: Type the message
        chat_input.click()
        time.sleep(0.3)
        chat_input.send_keys(message)
        time.sleep(0.3)

        # Step 4: Send — try Enter key first, then send button
        chat_input.send_keys(Keys.RETURN)
        log.info("Bot %d: Sent chat message%s.", bot_id + 1,
                 f" to '{recipient}'" if recipient else "")
        _take_screenshot(driver, bot_id, "message_sent")
        return True

    except Exception as exc:
        log.warning("Bot %d: Failed to send chat message: %s", bot_id + 1, exc)
        return False


# ── Leave-meeting selectors ────────────────────────────────────────────────
_LEAVE_BTN_SELECTORS = [
    (By.CSS_SELECTOR, "button[aria-label*='leave meeting' i]"),
    (By.CSS_SELECTOR, "button[aria-label*='leave' i]"),
    (By.CSS_SELECTOR, "button[data-testid='leave-btn']"),
    (By.XPATH, "//button[contains(@class, 'leave-meeting')]"),
    (By.XPATH, "//button[contains(@class, 'footer__leave-btn')]"),
    (By.CSS_SELECTOR, "button.footer__leave-btn"),
    (By.XPATH, "//button[contains(text(), 'Leave')]"),
    (By.XPATH, "//div[contains(text(), 'Leave meeting')]"),
]
_LEAVE_CONFIRM_SELECTORS = [
    (By.XPATH, "//button[contains(@class, 'leave-meeting-options__btn')]"),
    (By.XPATH, "//button[contains(text(), 'Leave Meeting')]"),
    (By.XPATH, "//button[contains(text(), 'Leave meeting')]"),
    (By.CSS_SELECTOR, "button.zm-btn--primary.leave-meeting-options__btn"),
    (By.CSS_SELECTOR, ".leave-meeting-options__btn"),
    (By.CSS_SELECTOR, "button[data-testid='leave-meeting-btn']"),
    (By.XPATH, "//button[contains(@class, 'zm-btn--primary')][contains(text(), 'Leave')]"),
]

LEAVE_TIMEOUT = 5


def leave_meeting(driver, bot_id):
    """Gracefully leave a Zoom meeting by clicking the Leave button.

    Returns True if the leave action was performed, False otherwise.
    The caller should still call driver.quit() after this.
    """
    try:
        driver.switch_to.default_content()

        # Switch into the Zoom iframe if needed
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for iframe in iframes:
            try:
                driver.switch_to.frame(iframe)
                if _find_element_multi(driver, _LEAVE_BTN_SELECTORS):
                    break
                driver.switch_to.default_content()
            except Exception:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

        # Step 1: Click the "Leave" button in the meeting toolbar
        leave_btn = _find_element_multi(driver, _LEAVE_BTN_SELECTORS)
        if not leave_btn:
            log.debug("Bot %d: Leave button not found, skipping graceful leave.", bot_id)
            return False

        driver.execute_script("arguments[0].click();", leave_btn)
        log.info("Bot %d: Clicked leave button.", bot_id)
        time.sleep(1)

        # Step 2: Click "Leave Meeting" on the confirmation dialog
        confirm_btn = _find_element_multi(driver, _LEAVE_CONFIRM_SELECTORS)
        if confirm_btn:
            driver.execute_script("arguments[0].click();", confirm_btn)
            log.info("Bot %d: Confirmed leave meeting.", bot_id)
            time.sleep(1)
        else:
            log.debug("Bot %d: No leave confirmation dialog, leave may have completed directly.", bot_id)

        return True

    except Exception as exc:
        log.debug("Bot %d: Error during graceful leave: %s", bot_id, exc)
        return False


def _click_join(driver, bot_id, bot_name, passcode):
    """Locate and click the Zoom join button."""
    join_btn = _find_element_multi(driver, _JOIN_SELECTORS)

    if not join_btn:
        # Wait a bit longer and retry
        for by, sel in _JOIN_SELECTORS:
            try:
                join_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((by, sel))
                )
                break
            except TimeoutException:
                continue

    if not join_btn:
        # JS fallback
        join_btn = _find_element_js(driver, 'join')

    if not join_btn:
        raise RuntimeError("Could not find join button")

    try:
        driver.execute_script("arguments[0].click();", join_btn)
    except ElementClickInterceptedException:
        log.info("Bot %d: Join click intercepted, retrying…", bot_id + 1)
        if not _verify_input_fields(driver, bot_name, passcode):
            raise RuntimeError("Input fields validation failed")
        driver.execute_script("arguments[0].click();", join_btn)
