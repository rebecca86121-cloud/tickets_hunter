#encoding=utf-8
# =============================================================================
# NOL World (놀티켓) Platform Module - International Version
# Target: https://world.nol.com/zh-CN/ticket
# Formerly known as Interpark Global
# =============================================================================

import asyncio
import base64
from datetime import datetime
import json
import os
import random
import re
import time
import urllib.parse

from zendriver import cdp

try:
    import ddddocr
except Exception:
    ddddocr = None

try:
    import cv2
    import numpy as np
except Exception:
    cv2 = None
    np = None

import util
from nodriver_common import (
    check_and_handle_pause,
    nodriver_check_checkbox,
    nodriver_get_text_by_selector,
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
    CONST_FROM_TOP_TO_BOTTOM,
)

# Module-level OCR instance (lazy init)
_ocr_instance = None

__all__ = [
    "nodriver_nol_signin",
    "nodriver_nol_main",
]

# Module-level state
_state = {}
_last_login_attempt = 0        # timestamp of last login attempt (to throttle)
_login_no_cred_warned = False  # only warn once about missing credentials

# NOL World URL patterns
CONST_NOL_LOGIN_URL = "https://world.nol.com/login?lang=zh-CN"
CONST_NOL_TICKET_URL = "https://world.nol.com/zh-CN/ticket"
# Event detail formats:
#   https://world.nol.com/zh-CN/ticket/places/{placeCode}/products/{goodsCode}
#   https://world.nol.com/zh-CN/ticket/genre/CONCERT/products/{goodsCode}?placeCode={placeCode}


def _is_nol_domain(url):
    """Check if URL belongs to NOL/Interpark ecosystem."""
    return 'nol.com' in url or 'interpark.com' in url


def _is_nol_login_page(url):
    """Check if current URL is a NOL login page."""
    return '/login' in url and 'nol.com' in url


def _is_nol_event_page(url):
    """Check if current URL is a NOL event detail page."""
    if 'nol.com' not in url:
        return False
    # Format 1: /ticket/places/{placeCode}/products/{goodsCode}
    if '/ticket/places/' in url and '/products/' in url:
        return True
    # Format 2: /ticket/genre/.../products/{goodsCode}?placeCode=...
    if '/ticket/' in url and '/products/' in url:
        return True
    return False


def _is_nol_homepage(url):
    """Check if on NOL homepage (not on event/login/booking page)."""
    if 'nol.com' not in url:
        return False
    if _is_nol_login_page(url):
        return False
    if _is_nol_event_page(url):
        return False
    # Don't treat my-info/reservations/order pages as homepage
    if '/my-info' in url or '/reservations' in url or '/order' in url or '/checkout' in url:
        return False
    return True


# Interpark onestop booking flow pages
def _is_nol_onestop_schedule(url):
    """Check if on date/schedule selection page."""
    return 'interpark.com' in url and '/onestop/schedule' in url


def _is_nol_onestop_seat(url):
    """Check if on seat selection page (NOT price step)."""
    return 'interpark.com' in url and '/onestop/seat' in url and 'step=price' not in url


def _is_nol_onestop_price(url):
    """Check if on price/quantity selection page (seat?step=price)."""
    return 'interpark.com' in url and '/onestop/seat' in url and 'step=price' in url


def _is_nol_onestop_checkout(url):
    """Check if on checkout/payment page."""
    return 'interpark.com' in url and ('/onestop/order' in url or '/onestop/payment' in url or '/onestop/checkout' in url or '/onestop/confirm' in url)


def _is_nol_onestop_page(url):
    """Check if on any interpark onestop booking page."""
    return 'interpark.com' in url and '/onestop/' in url


def _is_nol_booking_page(url):
    """Check if current URL is a NOL booking/checkout page."""
    if _is_nol_onestop_checkout(url):
        return True
    return ('booking' in url or 'checkout' in url or 'order' in url) and _is_nol_domain(url)


def _is_nol_seat_selection_page(url):
    """Check if current URL is a NOL seat selection page."""
    if _is_nol_onestop_seat(url):
        return True
    return ('seat' in url or 'book' in url) and _is_nol_domain(url)


# ---- Old-style globalinterpark.com booking flow ----
def _is_gpo_booking_page(url):
    """Check if on gpoticket.globalinterpark.com booking page."""
    return 'globalinterpark.com' in url and '/Book/' in url

def _is_gpo_waiting_page(url):
    """Check if on globalinterpark.com waiting page."""
    return 'globalinterpark.com' in url and 'Waiting' in url


async def nodriver_nol_signin(tab, url, config_dict):
    """Handle NOL World login — simulate real human typing via CDP key events."""
    global _last_login_attempt, _login_no_cred_warned

    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)

    nol_account = config_dict["accounts"].get("nol_account", "").strip()
    nol_password = config_dict["accounts"].get("nol_password", "").strip()

    # Strict check: no credentials → do nothing
    if len(nol_account) < 4 or len(nol_password) < 1:
        if not _login_no_cred_warned:
            debug.log("[NOL] ⚠️ 帳號或密碼未設定，請到設定頁面填寫")
            _login_no_cred_warned = True
        return False

    # Throttle: at least 5 seconds between login attempts (avoid repeated refill)
    now_ts = time.time()
    if now_ts - _last_login_attempt < 5.0:
        return False
    _last_login_attempt = now_ts

    print("[NOL] nodriver_nol_signin:", url)

    try:
        # Step 1: Wait for page fully loaded + input fields ready
        for wait_i in range(40):  # max 8 seconds
            input_info = await tab.evaluate('''
                (function() {
                    if (document.readyState !== 'complete') return 'loading';
                    const inputs = document.querySelectorAll('input:not([type="hidden"])');
                    if (inputs.length < 2) return 'no_inputs:' + inputs.length;
                    const pwd = document.querySelector('input[type="password"]');
                    if (!pwd) return 'no_password';
                    return 'ready:' + inputs.length;
                })()
            ''')
            if input_info and str(input_info).startswith('ready'):
                debug.log(f"[NOL] Page ready: {input_info} ({wait_i * 0.2:.1f}s)")
                break
            await asyncio.sleep(0.2)
        else:
            debug.log(f"[NOL] Page not ready after 8s: {input_info}")
            return False

        # Wait for React hydration
        await asyncio.sleep(1.5)

        # Helper: focus + clear an input field
        async def _focus_and_clear(selector_js):
            """Focus an input via JS selector, then Ctrl+A + Backspace to clear."""
            await tab.evaluate(selector_js)
            await asyncio.sleep(0.2)
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="a", code="KeyA",
                windows_virtual_key_code=65, native_virtual_key_code=65,
                modifiers=2
            ))
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="a", code="KeyA",
                windows_virtual_key_code=65, native_virtual_key_code=65,
                modifiers=2
            ))
            await asyncio.sleep(0.05)
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="Backspace", code="Backspace",
                windows_virtual_key_code=8, native_virtual_key_code=8
            ))
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="Backspace", code="Backspace",
                windows_virtual_key_code=8, native_virtual_key_code=8
            ))
            await asyncio.sleep(0.1)

        # Step 2: Check if fields are ALREADY filled (avoid redundant refill)
        field_status = await tab.evaluate('''
            (function() {
                const emailEl = document.querySelector('input[placeholder*="email" i]')
                    || document.querySelector('input[type="email"]')
                    || document.querySelectorAll('input:not([type="hidden"]):not([type="password"]):not([type="checkbox"]):not([type="radio"])')[0];
                const pwdEl = document.querySelector('input[type="password"]');
                const elen = emailEl ? emailEl.value.length : -1;
                const plen = pwdEl ? pwdEl.value.length : -1;
                return elen + ',' + plen;
            })()
        ''')
        print(f"[NOL] Current field status: {field_status}")

        already_filled = False
        if field_status:
            parts = str(field_status).split(',')
            if len(parts) == 2:
                try:
                    elen, plen = int(parts[0]), int(parts[1])
                    if elen >= 4 and plen >= 1:
                        print("[NOL] Fields already filled, skipping to Turnstile + login")
                        already_filled = True
                except ValueError:
                    pass

        if not already_filled:
            # Fill email using CDP insertText
            focus_email_js = '''
                (function() {
                    const selectors = [
                        'input[placeholder*="email" i]',
                        'input[type="email"]',
                        'input[autocomplete="email"]',
                        'input[autocomplete="username"]',
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el && el.offsetHeight > 0) { el.focus(); el.click(); return 'found'; }
                    }
                    const inputs = document.querySelectorAll('input:not([type="hidden"]):not([type="password"]):not([type="checkbox"]):not([type="radio"])');
                    for (const inp of inputs) {
                        if (inp.offsetHeight > 0) { inp.focus(); inp.click(); return 'fallback'; }
                    }
                    return 'not_found';
                })()
            '''
            email_result = await tab.evaluate(focus_email_js)
            debug.log(f"[NOL] Email input: {email_result}")
            if 'not_found' in str(email_result):
                debug.log("[NOL] Cannot find email input")
                return False

            await _focus_and_clear(focus_email_js)
            await tab.send(cdp.input_.insert_text(text=nol_account))
            await asyncio.sleep(0.3)

            # Verify email
            email_len = await tab.evaluate('''
                (function() {
                    const el = document.querySelector('input[placeholder*="email" i]')
                        || document.querySelector('input[type="email"]')
                        || document.querySelectorAll('input:not([type="hidden"]):not([type="password"])')[0];
                    return el ? el.value.length : -1;
                })()
            ''')
            print(f"[NOL] Email length: {email_len}")
            if not email_len or int(email_len) < 4:
                print("[NOL] ❌ Email fill failed, NOT clicking login")
                return False

            # Fill password
            pwd_focus_js = '''
                (function() {
                    const pwd = document.querySelector('input[type="password"]');
                    if (pwd) { pwd.focus(); pwd.click(); return 'ok'; }
                    return 'not_found';
                })()
            '''
            await _focus_and_clear(pwd_focus_js)
            await tab.send(cdp.input_.insert_text(text=nol_password))
            await asyncio.sleep(0.3)

            # Verify password
            pwd_len = await tab.evaluate('''
                (function() {
                    const el = document.querySelector('input[type="password"]');
                    return el ? el.value.length : -1;
                })()
            ''')
            print(f"[NOL] Password length: {pwd_len}")
            if not pwd_len or int(pwd_len) < 1:
                print("[NOL] ❌ Password fill failed, NOT clicking login")
                return False

            print("[NOL] ✅ Both fields filled successfully")

        # Step 3: Click Cloudflare Turnstile checkbox, then wait for it to complete
        print("[NOL] Looking for Turnstile checkbox...")

        # Find and click the Turnstile checkbox via CDP mouse click
        turnstile_clicked = await tab.evaluate('''
            (function() {
                // Look for Turnstile container
                const cfDiv = document.querySelector('.cf-turnstile, [class*="turnstile"]');
                if (cfDiv) {
                    const iframe = cfDiv.querySelector('iframe');
                    if (iframe) {
                        const rect = iframe.getBoundingClientRect();
                        return JSON.stringify({found: true, x: Math.round(rect.x + 30), y: Math.round(rect.y + rect.height / 2)});
                    }
                    // No iframe yet, try clicking the div itself
                    const rect = cfDiv.getBoundingClientRect();
                    if (rect.width > 0) {
                        return JSON.stringify({found: true, x: Math.round(rect.x + 30), y: Math.round(rect.y + rect.height / 2)});
                    }
                }
                // Try finding any iframe with turnstile in src
                const iframes = document.querySelectorAll('iframe');
                for (const iframe of iframes) {
                    const src = iframe.src || '';
                    if (src.includes('turnstile') || src.includes('cloudflare') || src.includes('challenges')) {
                        const rect = iframe.getBoundingClientRect();
                        if (rect.width > 0) {
                            return JSON.stringify({found: true, x: Math.round(rect.x + 30), y: Math.round(rect.y + rect.height / 2)});
                        }
                    }
                }
                return JSON.stringify({found: false});
            })()
        ''')
        print(f"[NOL] Turnstile location: {turnstile_clicked}")

        try:
            tc_info = json.loads(turnstile_clicked) if isinstance(turnstile_clicked, str) else {}
        except Exception:
            tc_info = {}

        if tc_info.get('found'):
            tx, ty = tc_info['x'], tc_info['y']
            print(f"[NOL] Clicking Turnstile checkbox at ({tx}, {ty})...")
            await tab.send(cdp.input_.dispatch_mouse_event(
                type_="mousePressed", x=tx, y=ty, button=cdp.input_.MouseButton.LEFT, click_count=1
            ))
            await tab.send(cdp.input_.dispatch_mouse_event(
                type_="mouseReleased", x=tx, y=ty, button=cdp.input_.MouseButton.LEFT, click_count=1
            ))
            await asyncio.sleep(1.0)
        else:
            print("[NOL] Turnstile checkbox not found, proceeding...")

        # Wait for Turnstile token to be ready
        print("[NOL] Waiting for Turnstile to complete...")
        turnstile_ready = False
        for tw in range(30):  # max 15 seconds
            ts_status = await tab.evaluate('''
                (function() {
                    // Check for Turnstile response token (hidden input)
                    const tokenInput = document.querySelector('input[name="cf-turnstile-response"]')
                        || document.querySelector('[name="cf-turnstile-response"]');
                    if (tokenInput && tokenInput.value && tokenInput.value.length > 10) {
                        return 'ready:' + tokenInput.value.length;
                    }
                    // Check if Turnstile iframe shows checkmark (completed state)
                    const cfDiv = document.querySelector('.cf-turnstile, [class*="turnstile"]');
                    if (cfDiv) {
                        const iframe = cfDiv.querySelector('iframe');
                        if (!iframe) return 'no_iframe';
                        return 'waiting';
                    }
                    // Try checking any iframe
                    const iframes = document.querySelectorAll('iframe');
                    for (const iframe of iframes) {
                        const src = iframe.src || '';
                        if (src.includes('turnstile') || src.includes('cloudflare') || src.includes('challenges')) {
                            return 'waiting_iframe';
                        }
                    }
                    return 'no_turnstile';
                })()
            ''')
            if ts_status and 'ready' in str(ts_status):
                print(f"[NOL] ✅ Turnstile completed: {ts_status}")
                turnstile_ready = True
                break
            if 'no_turnstile' in str(ts_status) and tw >= 6:
                # After 3 seconds, if still no turnstile detected, give up waiting
                print(f"[NOL] Turnstile not found after {tw * 0.5:.1f}s, proceeding...")
                break
            if tw % 5 == 0:
                print(f"[NOL] Turnstile: {ts_status} ({tw * 0.5:.1f}s)")
            await asyncio.sleep(0.5)

        if not turnstile_ready:
            print("[NOL] ⏳ Turnstile not ready, trying to click login anyway")

        # Step 4: Click login button
        print("[NOL] Clicking login button...")
        login_clicked = await tab.evaluate('''
            (function() {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    const text = btn.textContent.trim();
                    if (text === '登录' || text === '登錄' || text === 'Log in' || text === '로그인' ||
                        text === '登入' || text === 'Sign in' || text === 'Login') {
                        btn.click();
                        return 'ok:' + text;
                    }
                }
                const submitBtn = document.querySelector('button[type="submit"]');
                if (submitBtn) { submitBtn.click(); return 'ok:submit'; }
                return 'button_not_found';
            })()
        ''')
        print(f"[NOL] Login click: {login_clicked}")

        if not login_clicked or not str(login_clicked).startswith('ok'):
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="Enter", code="Enter",
                windows_virtual_key_code=13, native_virtual_key_code=13
            ))
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="Enter", code="Enter",
                windows_virtual_key_code=13, native_virtual_key_code=13
            ))
            print("[NOL] Pressed Enter as fallback")

        # Wait for login redirect
        max_wait = 10
        check_interval = 0.3
        max_attempts = int(max_wait / check_interval)

        for attempt in range(max_attempts):
            if await check_and_handle_pause(config_dict):
                return False
            try:
                current_url = await tab.evaluate('window.location.href')
                if not _is_nol_login_page(current_url):
                    debug.log(f"[NOL] Login OK in {attempt * check_interval:.1f}s → {current_url}")

                    # Navigate to target event page
                    homepage = config_dict.get("homepage", "")
                    if homepage and 'nol.com' in homepage and not _is_nol_event_page(current_url):
                        debug.log(f"[NOL] → Navigating to: {homepage}")
                        try:
                            await tab.get(homepage)
                        except Exception as e:
                            debug.log(f"[NOL] Navigation error: {e}")
                    return True
            except Exception:
                pass
            await asyncio.sleep(check_interval)

        debug.log("[NOL] Login timeout after 10s")
        return False

    except Exception as e:
        debug.log(f"[NOL] Login error: {e}")
        return False


async def _nol_handle_event_page(tab, url, config_dict):
    """Handle NOL event detail page - click 'Buy Now' button."""
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    debug.log("[NOL] On event page:", url)

    await asyncio.sleep(random.uniform(0.3, 0.8))

    # Check if the event sale has started
    try:
        page_text = await tab.evaluate('document.body.innerText')
        if page_text:
            # Check for "sold out" or "not available" indicators
            sold_out_keywords = ['sold out', '已售完', '售罄', '매진', 'unavailable']
            for keyword in sold_out_keywords:
                if keyword.lower() in page_text.lower():
                    debug.log(f"[NOL] Event appears sold out (found: {keyword})")
                    # Auto reload if configured
                    reload_interval = config_dict.get("advanced", {}).get("auto_reload_page_interval", 5)
                    if reload_interval > 0:
                        debug.log(f"[NOL] Auto-reloading in {reload_interval} seconds...")
                        await asyncio.sleep(reload_interval)
                        await tab.reload()
                    return False
    except Exception as e:
        debug.log(f"[NOL] Error checking page text: {e}")

    # Handle Cloudflare Turnstile if present (random challenge on product page)
    try:
        turnstile_result = await tab.evaluate('''
            (function() {
                // Look for Turnstile checkbox iframe
                const cfDiv = document.querySelector('.cf-turnstile, [class*="turnstile"]');
                if (cfDiv) {
                    const iframe = cfDiv.querySelector('iframe');
                    if (iframe) {
                        const rect = iframe.getBoundingClientRect();
                        return JSON.stringify({found: true, x: Math.round(rect.x + 30), y: Math.round(rect.y + rect.height / 2), type: 'cf-div'});
                    }
                    const rect = cfDiv.getBoundingClientRect();
                    if (rect.width > 0) {
                        return JSON.stringify({found: true, x: Math.round(rect.x + 30), y: Math.round(rect.y + rect.height / 2), type: 'cf-div-no-iframe'});
                    }
                }
                // Check for standalone turnstile iframe
                const iframes = document.querySelectorAll('iframe');
                for (const ifr of iframes) {
                    const src = (ifr.src || '').toLowerCase();
                    if (src.includes('turnstile') || src.includes('challenges.cloudflare')) {
                        const rect = ifr.getBoundingClientRect();
                        if (rect.width > 0) {
                            return JSON.stringify({found: true, x: Math.round(rect.x + 30), y: Math.round(rect.y + rect.height / 2), type: 'iframe'});
                        }
                    }
                }
                return JSON.stringify({found: false});
            })()
        ''')
        ts_info = json.loads(turnstile_result) if isinstance(turnstile_result, str) else {}
        if ts_info.get('found'):
            tx, ty = ts_info['x'], ts_info['y']
            debug.log(f"[NOL] Turnstile on product page at ({tx},{ty}), clicking via CDP...")
            # Use CDP mouse events (same as login page) — can click inside cross-origin iframes
            await tab.send(cdp.input_.dispatch_mouse_event(
                type_="mousePressed", x=tx, y=ty, button=cdp.input_.MouseButton.LEFT, click_count=1
            ))
            await tab.send(cdp.input_.dispatch_mouse_event(
                type_="mouseReleased", x=tx, y=ty, button=cdp.input_.MouseButton.LEFT, click_count=1
            ))
            await asyncio.sleep(1.0)

            # Wait for Turnstile to complete
            debug.log("[NOL] Waiting for Turnstile to complete...")
            for tw in range(30):  # max 15 seconds
                ts_status = await tab.evaluate('''
                    (function() {
                        const tokenInput = document.querySelector('input[name="cf-turnstile-response"]')
                            || document.querySelector('[name="cf-turnstile-response"]');
                        if (tokenInput && tokenInput.value && tokenInput.value.length > 10) {
                            return 'ready:' + tokenInput.value.length;
                        }
                        const cfDiv = document.querySelector('.cf-turnstile, [class*="turnstile"]');
                        if (!cfDiv) return 'no_turnstile';
                        return 'waiting';
                    })()
                ''')
                if 'ready' in str(ts_status):
                    debug.log(f"[NOL] ✅ Turnstile completed: {ts_status}")
                    await asyncio.sleep(0.5)
                    break
                if 'no_turnstile' in str(ts_status) and tw >= 6:
                    debug.log("[NOL] Turnstile element gone, proceeding")
                    break
                await asyncio.sleep(0.5)
            else:
                debug.log("[NOL] ⚠️ Turnstile not completed in 15s, proceeding anyway")
    except Exception as e:
        debug.log(f"[NOL] Turnstile check error: {e}")

    # Try to find and click the "Buy Now" button
    buy_clicked = False
    buy_selectors = [
        'button:has-text("Buy now")',
        'button:has-text("立即购买")',
        'button:has-text("지금 구매")',
        'button:has-text("購入")',
        'a:has-text("Buy now")',
        'a:has-text("立即购买")',
        '[class*="purchase"]',
        '[class*="buy"]',
        '[class*="booking"]',
    ]

    for selector in buy_selectors:
        try:
            buy_btn = await tab.query_selector(selector)
            if buy_btn:
                await buy_btn.click()
                buy_clicked = True
                debug.log(f"[NOL] 'Buy Now' clicked via: {selector}")
                break
        except Exception:
            continue

    if not buy_clicked:
        # JavaScript fallback to find Buy/Purchase button
        try:
            result = await tab.evaluate('''
                (function() {
                    // Try buttons
                    const btns = document.querySelectorAll('button, a');
                    for (const btn of btns) {
                        const text = btn.textContent.trim().toLowerCase();
                        if (text.includes('buy now') || text.includes('立即购买') ||
                            text.includes('purchase') || text.includes('book') ||
                            text.includes('지금 구매') || text.includes('예매')) {
                            btn.click();
                            return 'clicked: ' + text;
                        }
                    }
                    return 'not_found';
                })()
            ''')
            if result and 'clicked' in str(result):
                buy_clicked = True
                debug.log(f"[NOL] Buy button clicked via JS: {result}")
        except Exception as e:
            debug.log(f"[NOL] JS buy button click failed: {e}")

    if buy_clicked:
        debug.log("[NOL] Waiting for booking page to load...")
        await asyncio.sleep(random.uniform(1.0, 2.0))
        # Play sound to notify user
        play_sound_while_ordering(config_dict)

    return buy_clicked


async def _nol_handle_date_selection(tab, url, config_dict):
    """Handle date/time selection on NOL booking page."""
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    debug.log("[NOL] Handling date selection...")

    date_keyword = config_dict.get("date_auto_select", {}).get("date_keyword", "")
    date_keywords = [k.strip() for k in date_keyword.split(";") if k.strip()] if date_keyword else []

    await asyncio.sleep(random.uniform(0.3, 0.6))

    try:
        # NOL uses various date selection mechanisms
        # Try to find date/session options
        date_items = await tab.evaluate('''
            (function() {
                const items = [];
                // Look for date selection elements
                const selectors = [
                    'li[class*="date"]', 'li[class*="schedule"]',
                    'div[class*="date"]', 'div[class*="session"]',
                    'button[class*="date"]', 'button[class*="session"]',
                    '[class*="performance"]', '[class*="showtime"]',
                    'ul.schedule li', '.schedule-item',
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    els.forEach((el, i) => {
                        items.push({
                            index: i,
                            text: el.textContent.trim(),
                            selector: sel,
                            disabled: el.classList.contains('disabled') || el.classList.contains('sold-out'),
                        });
                    });
                    if (items.length > 0) break;
                }
                return items;
            })()
        ''')

        if date_items and len(date_items) > 0:
            debug.log(f"[NOL] Found {len(date_items)} date options")

            target_index = -1
            # Try to match by keyword
            if date_keywords:
                for i, item in enumerate(date_items):
                    if item.get('disabled'):
                        continue
                    item_text = item.get('text', '')
                    for keyword in date_keywords:
                        if keyword in item_text:
                            target_index = i
                            debug.log(f"[NOL] Date matched keyword '{keyword}': {item_text}")
                            break
                    if target_index >= 0:
                        break

            # If no keyword match, select first available
            if target_index < 0:
                for i, item in enumerate(date_items):
                    if not item.get('disabled'):
                        target_index = i
                        debug.log(f"[NOL] Auto-selecting first available date: {item.get('text', '')}")
                        break

            if target_index >= 0:
                selector = date_items[target_index].get('selector', '')
                await tab.evaluate(f'''
                    (function() {{
                        const els = document.querySelectorAll('{selector}');
                        if (els[{target_index}]) {{
                            els[{target_index}].click();
                        }}
                    }})()
                ''')
                debug.log(f"[NOL] Date selected: index {target_index}")
                await asyncio.sleep(random.uniform(0.3, 0.5))
                return True

        debug.log("[NOL] No date options found, page may use different structure")
        return False

    except Exception as e:
        debug.log(f"[NOL] Date selection error: {e}")
        return False


async def _nol_handle_seat_selection(tab, url, config_dict):
    """Handle seat/area selection on NOL booking page."""
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    debug.log("[NOL] Handling seat/area selection...")

    area_keyword = config_dict.get("area_auto_select", {}).get("area_keyword", "")
    area_keywords = [k.strip() for k in area_keyword.split(";") if k.strip()] if area_keyword else []
    ticket_number = config_dict.get("ticket_number", 1)

    await asyncio.sleep(random.uniform(0.3, 0.6))

    try:
        # Try to find seat/area options
        area_result = await tab.evaluate('''
            (function() {
                const areas = [];
                // Look for area/seat type options
                const selectors = [
                    '[class*="seat-grade"]', '[class*="price-grade"]',
                    '[class*="ticket-type"]', '[class*="grade"]',
                    'div[class*="section"]', 'li[class*="area"]',
                    '.seat-list a', '.list a',
                    'td img[class*="seat"]',
                ];
                for (const sel of selectors) {
                    const els = document.querySelectorAll(sel);
                    els.forEach((el, i) => {
                        areas.push({
                            index: i,
                            text: el.textContent.trim() || el.alt || '',
                            selector: sel,
                            available: !el.classList.contains('disabled') && !el.classList.contains('sold-out'),
                        });
                    });
                    if (areas.length > 0) break;
                }
                return areas;
            })()
        ''')

        if area_result and len(area_result) > 0:
            debug.log(f"[NOL] Found {len(area_result)} seat/area options")

            target_index = -1
            # Match by keyword
            if area_keywords:
                for i, area in enumerate(area_result):
                    if not area.get('available', True):
                        continue
                    area_text = area.get('text', '')
                    for keyword in area_keywords:
                        if keyword.lower() in area_text.lower():
                            target_index = i
                            debug.log(f"[NOL] Area matched keyword '{keyword}': {area_text}")
                            break
                    if target_index >= 0:
                        break

            # First available fallback
            if target_index < 0:
                for i, area in enumerate(area_result):
                    if area.get('available', True):
                        target_index = i
                        debug.log(f"[NOL] Auto-selecting first available area: {area.get('text', '')}")
                        break

            if target_index >= 0:
                selector = area_result[target_index].get('selector', '')
                await tab.evaluate(f'''
                    (function() {{
                        const els = document.querySelectorAll('{selector}');
                        if (els[{target_index}]) {{
                            els[{target_index}].click();
                        }}
                    }})()
                ''')
                debug.log(f"[NOL] Area selected: index {target_index}")
                await asyncio.sleep(random.uniform(0.5, 1.0))

        # Try to set ticket quantity
        await _nol_set_ticket_quantity(tab, ticket_number, debug)

        return True

    except Exception as e:
        debug.log(f"[NOL] Seat selection error: {e}")
        return False


async def _nol_set_ticket_quantity(tab, ticket_number, debug):
    """Set the number of tickets to purchase."""
    try:
        # Try select dropdown first
        result = await tab.evaluate(f'''
            (function() {{
                // Try select element
                const selects = document.querySelectorAll('select');
                for (const sel of selects) {{
                    const name = (sel.name || sel.id || '').toLowerCase();
                    if (name.includes('count') || name.includes('quantity') || name.includes('ticket') || name.includes('seat')) {{
                        sel.value = '{ticket_number}';
                        sel.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return 'select: ' + name;
                    }}
                }}

                // Try number input
                const inputs = document.querySelectorAll('input[type="number"]');
                for (const inp of inputs) {{
                    const name = (inp.name || inp.id || inp.placeholder || '').toLowerCase();
                    if (name.includes('count') || name.includes('quantity') || name.includes('ticket') || name.includes('num')) {{
                        inp.value = {ticket_number};
                        inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return 'input: ' + name;
                    }}
                }}

                // Try +/- buttons
                const addBtns = document.querySelectorAll('[class*="plus"], [class*="add"], [class*="increase"]');
                if (addBtns.length > 0) {{
                    for (let i = 1; i < {ticket_number}; i++) {{
                        addBtns[0].click();
                    }}
                    return 'plus_button';
                }}

                return 'not_found';
            }})()
        ''')
        if result:
            debug.log(f"[NOL] Ticket quantity set to {ticket_number} via: {result}")
    except Exception as e:
        debug.log(f"[NOL] Set ticket quantity error: {e}")


async def _nol_handle_onestop_price(tab, url, config_dict):
    """Handle Interpark onestop price/quantity page (seat?step=price).
    This page shows after seat selection. User selects ticket count and clicks '訂購'.
    """
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    print("[NOL] On price/quantity selection page (step=price)")

    try:
        # Kill beforeunload
        try:
            await tab.evaluate('''
                (function() {
                    window.onbeforeunload = null;
                    var origAdd = EventTarget.prototype.addEventListener;
                    EventTarget.prototype.addEventListener = function(type, fn, opts) {
                        if (type === 'beforeunload') return;
                        return origAdd.call(this, type, fn, opts);
                    };
                    Object.defineProperty(window, 'onbeforeunload', {
                        get: function() { return null; },
                        set: function() { },
                        configurable: true
                    });
                })()
            ''')
        except Exception:
            pass

        # Set up auto-accept for native browser dialogs
        async def _auto_accept_dialog_price(event: cdp.page.JavascriptDialogOpening):
            print(f"[NOL] Auto-accepting browser dialog: {event.message[:50]}")
            try:
                await tab.send(cdp.page.handle_java_script_dialog(accept=True))
            except Exception:
                pass

        try:
            tab.add_handler(cdp.page.JavascriptDialogOpening, _auto_accept_dialog_price)
        except Exception:
            pass

        await asyncio.sleep(1.0)

        # Get ticket number from config
        ticket_number = config_dict.get("ticket_number", 1)
        try:
            ticket_number = int(ticket_number)
            if ticket_number < 1:
                ticket_number = 1
        except (ValueError, TypeError):
            ticket_number = 1

        # Set ticket quantity by clicking "+" button
        print(f"[NOL] Setting ticket quantity to {ticket_number}...")
        for click_i in range(ticket_number):
            plus_result = await tab.evaluate('''
                (function() {
                    // Find the "+" button
                    const btns = document.querySelectorAll('button');
                    for (const btn of btns) {
                        const text = btn.textContent.trim();
                        if (text === '+' || text === '＋') {
                            btn.click();
                            return 'clicked_plus';
                        }
                    }
                    // Try SVG/icon plus buttons
                    const allBtns = document.querySelectorAll('button, [role="button"]');
                    for (const btn of allBtns) {
                        const cls = (btn.className || '').toLowerCase();
                        const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                        if (cls.includes('plus') || cls.includes('increase') || cls.includes('add') ||
                            ariaLabel.includes('plus') || ariaLabel.includes('increase') || ariaLabel.includes('add') ||
                            ariaLabel.includes('증가') || ariaLabel.includes('추가')) {
                            btn.click();
                            return 'clicked_plus_icon';
                        }
                    }
                    return 'plus_not_found';
                })()
            ''')
            print(f"[NOL] Plus button click {click_i + 1}/{ticket_number}: {plus_result}")
            await asyncio.sleep(0.3)

        # Verify quantity was set
        await asyncio.sleep(0.5)
        qty_check = await tab.evaluate('''
            (function() {
                // Check for quantity display
                const allEls = document.querySelectorAll('span, div, input');
                for (const el of allEls) {
                    const text = el.textContent ? el.textContent.trim() : (el.value || '');
                    const prev = el.previousElementSibling;
                    const next = el.nextElementSibling;
                    // Look for a number between +/- buttons
                    if (/^\\d+$/.test(text) && parseInt(text) >= 0) {
                        const prevText = prev ? prev.textContent.trim() : '';
                        const nextText = next ? next.textContent.trim() : '';
                        if ((prevText === '-' || prevText === '－') && (nextText === '+' || nextText === '＋') ||
                            (prevText === '+' || prevText === '＋') && (nextText === '-' || nextText === '－')) {
                            return 'qty:' + text;
                        }
                    }
                }
                // Check price display
                const priceEls = document.querySelectorAll('[class*="price" i], [class*="total" i]');
                for (const el of priceEls) {
                    const text = el.textContent.trim();
                    if (text.includes('元') || text.includes('원')) return 'price:' + text.substring(0, 20);
                }
                return 'unknown';
            })()
        ''')
        print(f"[NOL] Quantity check: {qty_check}")

        # Check page info
        page_info = await tab.evaluate('''
            (function() {
                const btns = document.querySelectorAll('button, a, input[type="submit"]');
                const btnTexts = [];
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim();
                    if (text.length > 0 && text.length < 20) btnTexts.push(text);
                }
                return JSON.stringify({buttons: btnTexts});
            })()
        ''')
        print(f"[NOL] Price page buttons: {page_info}")

        # Click "訂購" button (NOT "訂購確認 ／ 取消")
        order_result = await tab.evaluate('''
            (function() {
                const btns = document.querySelectorAll('button, a, input[type="submit"]');

                // Priority 1: exact "訂購" button (not "訂購確認")
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim();
                    if (text === '訂購' || text === '订购' || text === '예매' || text === '예매하기') {
                        btn.click();
                        return 'clicked: ' + text;
                    }
                }

                // Priority 2: "Order" / "Purchase" / "Buy"
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim();
                    const textLower = text.toLowerCase();
                    if (textLower === 'order' || textLower === 'purchase' || textLower === 'buy' ||
                        text === '購買' || text === '购买' || text === '구매') {
                        btn.click();
                        return 'clicked: ' + text;
                    }
                }

                // Priority 3: "Next" / "下一步"
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim();
                    const textLower = text.toLowerCase();
                    if (text === '下一步' || textLower === 'next' || text === '다음' ||
                        text === '繼續' || text === '继续') {
                        btn.click();
                        return 'clicked: ' + text;
                    }
                }

                return 'not_found';
            })()
        ''')
        print(f"[NOL] Order button: {order_result}")

        if order_result and 'clicked' in str(order_result):
            await asyncio.sleep(1.0)

            # Handle confirmation dialog if it appears
            try:
                for attempt in range(5):
                    dialog_result = await tab.evaluate('''
                        (function() {
                            const bodyText = document.body ? (document.body.innerText || '') : '';
                            if (bodyText.includes('確定要移動至訂購確認') || bodyText.includes('移動時會失去現在的訂購')
                                || bodyText.includes('確定') || bodyText.includes('确定')) {
                                const btns = document.querySelectorAll('button');
                                for (const btn of btns) {
                                    const text = btn.textContent.trim();
                                    if (text === '確認' || text === '确认' || text === '確定' || text === '确定') {
                                        btn.click();
                                        return 'confirmed: ' + text;
                                    }
                                }
                            }
                            return 'no_dialog';
                        })()
                    ''')
                    if dialog_result and 'confirmed' in str(dialog_result):
                        print(f"[NOL] ✅ Price confirmed: {dialog_result}")
                        await asyncio.sleep(2.0)
                        play_sound_while_ordering(config_dict)
                        break
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                print("[NOL] ✅ Page navigated after order click")
                play_sound_while_ordering(config_dict)
            except Exception as e:
                print(f"[NOL] Dialog note: {e}")

            print("[NOL] ✅ Order submitted from price page!")
            play_sound_while_ordering(config_dict)
            return True
        else:
            print("[NOL] ⚠️ Could not find order button on price page")
            return True

    except asyncio.CancelledError:
        print("[NOL] ✅ Page navigated during price handling")
        play_sound_while_ordering(config_dict)
        return True
    except Exception as e:
        print(f"[NOL] Price page error: {e}")
        return False


async def _nol_click_next_step(tab, debug):
    """Click 'Next' or 'Confirm' button to proceed.
    Priority: 完成選擇 > 선택 완료 > next/下一步 > 訂購確認
    '完成選擇' finalizes seat selection and goes to checkout.
    '訂購確認 ／ 取消' goes to order confirm/cancel page (wrong).
    """
    try:
        result = await tab.evaluate('''
            (function() {
                const btns = document.querySelectorAll('button, a, input[type="submit"]');

                // Priority 1: "完成選擇" / "完成选择" / "선택 완료" — finalize seat selection
                const priorityKeywords = ['完成選擇', '完成选择', '선택 완료', 'seat selection completed',
                                          'Complete Selection', 'complete selection'];
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim();
                    for (const kw of priorityKeywords) {
                        if (text.includes(kw)) {
                            btn.click();
                            return 'clicked: ' + text.substring(0, 30);
                        }
                    }
                }

                // Priority 2: "next" / "下一步" / "다음" / "continue"
                const nextKeywords = ['next', '下一步', '다음', 'continue', '继续', '繼續', 'proceed'];
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim();
                    const textLower = text.toLowerCase();
                    for (const kw of nextKeywords) {
                        if (text.includes(kw) || textLower.includes(kw.toLowerCase())) {
                            if (text.includes('取消')) continue;
                            btn.click();
                            return 'clicked: ' + text.substring(0, 30);
                        }
                    }
                }

                // Priority 3: "訂購確認" — only as last resort
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim();
                    if (text.includes('訂購確認') || text.includes('订购确认')) {
                        if (text.includes('取消') && !text.includes('確認') && !text.includes('确认')) continue;
                        btn.click();
                        return 'clicked: ' + text.substring(0, 30);
                    }
                }

                // Priority 4: generic confirm/submit
                const fallbackKeywords = ['confirm', '确认', '確認', 'submit', '提交'];
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim();
                    const textLower = text.toLowerCase();
                    for (const kw of fallbackKeywords) {
                        if (text.includes(kw) || textLower.includes(kw.toLowerCase())) {
                            if (text.includes('取消')) continue;
                            btn.click();
                            return 'clicked: ' + text.substring(0, 30);
                        }
                    }
                }

                return 'not_found';
            })()
        ''')
        print(f"[NOL] Next step: {result}")
        if result and 'clicked' in str(result):
            return True
        return False
    except Exception as e:
        print(f"[NOL] Click next step error: {e}")
        return False


def _get_ocr():
    """Get or create OCR instance for CAPTCHA solving.
    Try to use the universal custom ONNX model first (better accuracy),
    fall back to default ddddocr.
    """
    global _ocr_instance
    if _ocr_instance is None and ddddocr is not None:
        try:
            # Try universal custom model first (same approach as Tixcraft)
            model_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                     'assets', 'model', 'universal')
            onnx_path = os.path.join(model_dir, 'custom.onnx')
            charset_path = os.path.join(model_dir, 'charsets.json')
            if os.path.exists(onnx_path) and os.path.exists(charset_path):
                with open(charset_path, 'r') as f:
                    charset_data = json.load(f)
                _ocr_instance = ddddocr.DdddOcr(
                    show_ad=False,
                    import_onnx_path=onnx_path,
                    charsets_path=charset_path
                )
                print(f"[NOL] OCR initialized with universal model")
            else:
                _ocr_instance = ddddocr.DdddOcr(show_ad=False, beta=True)
                print(f"[NOL] OCR initialized with default ddddocr (beta)")
        except Exception as e:
            try:
                _ocr_instance = ddddocr.DdddOcr(show_ad=False)
                print(f"[NOL] OCR initialized with default ddddocr")
            except Exception as e2:
                print(f"[NOL] Failed to init ddddocr: {e2}")
    return _ocr_instance


def _preprocess_captcha_image(img_bytes):
    """Preprocess CAPTCHA image to improve OCR accuracy.
    Steps: grayscale → denoise → binarize → clean noise dots/lines.
    Returns cleaned PNG bytes.
    """
    if cv2 is None or np is None:
        return img_bytes  # No OpenCV, return original

    try:
        arr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return img_bytes

        # 1. Resize if too small (helps OCR accuracy)
        h, w = img.shape
        if w < 200:
            scale = 200 / w
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        # 2. Denoise — remove background noise while keeping text edges
        denoised = cv2.fastNlMeansDenoising(img, h=25, templateWindowSize=7, searchWindowSize=21)

        # 3. Increase contrast
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        contrast = clahe.apply(denoised)

        # 4. Binarize (Otsu's automatic thresholding)
        _, binary = cv2.threshold(contrast, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # 5. Morphological open to remove small noise dots
        kernel = np.ones((2, 2), np.uint8)
        cleaned = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)

        # 6. Slight dilation to make text thicker/clearer
        kernel_dilate = np.ones((1, 1), np.uint8)
        cleaned = cv2.dilate(cleaned, kernel_dilate, iterations=1)

        # 7. Invert if text is white on dark background (OCR prefers black on white)
        # Check: if most pixels are dark, text is probably light → invert
        white_ratio = np.sum(cleaned > 127) / cleaned.size
        if white_ratio < 0.4:
            cleaned = cv2.bitwise_not(cleaned)

        _, result = cv2.imencode('.png', cleaned)
        return result.tobytes()

    except Exception as e:
        print(f"[NOL] CAPTCHA preprocess error: {e}")
        return img_bytes


def _preprocess_gpo_captcha(img_bytes):
    """Specialized preprocessing for GPO Interpark CAPTCHA.
    CAPTCHA style varies: colored text (green/yellow/white/cyan) on dark background
    with colored noise dots. Text color changes every time!
    Strategy:
    1. Text is always BRIGHTER and MORE SATURATED than background noise dots
    2. Use saturation channel (S in HSV) — text has high saturation, bg dots are dimmer
    3. Combine with value channel for brightness
    4. Connected component analysis to remove small noise
    """
    if cv2 is None or np is None:
        return _preprocess_captcha_image(img_bytes)

    try:
        arr = np.frombuffer(img_bytes, np.uint8)
        img_color = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img_color is None:
            return _preprocess_captcha_image(img_bytes)

        h, w = img_color.shape[:2]

        # Scale up 3x for better OCR
        scale = 3
        img_color = cv2.resize(img_color, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
        h, w = img_color.shape[:2]

        # Convert to HSV
        hsv = cv2.cvtColor(img_color, cv2.COLOR_BGR2HSV)
        h_ch, s_ch, v_ch = cv2.split(hsv)

        # Convert to grayscale
        gray = cv2.cvtColor(img_color, cv2.COLOR_BGR2GRAY)

        # Method A: High saturation + high value = vivid colored text
        # The text is always vivid (high saturation AND high brightness)
        # Noise dots are small and dimmer
        sat_thresh = np.percentile(s_ch, 75)  # Top 25% saturation
        val_thresh = np.percentile(v_ch, 70)  # Top 30% brightness
        mask_vivid = ((s_ch > max(sat_thresh, 80)) & (v_ch > max(val_thresh, 100))).astype(np.uint8) * 255

        # Method B: Just use brightness — text is brighter than background
        bright_thresh = np.percentile(gray, 80)
        mask_bright = (gray > max(bright_thresh, 120)).astype(np.uint8) * 255

        # Method C: Combine saturation and brightness
        combined_score = (s_ch.astype(float) * 0.5 + v_ch.astype(float) * 0.5)
        combined_thresh = np.percentile(combined_score, 75)
        mask_combined = (combined_score > combined_thresh).astype(np.uint8) * 255

        # Try each method, pick the one that produces best text-like result
        best_img = None
        best_score = -1

        for name, mask in [('vivid', mask_vivid), ('bright', mask_bright), ('combined', mask_combined)]:
            # Connected component analysis to remove noise
            num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)

            min_area = 20 * (scale * scale)
            max_area = w * h * 0.12
            min_h = 6 * scale
            max_h = h * 0.85

            # Keep only text-sized components
            clean = np.zeros_like(mask)
            kept = 0
            for i in range(1, num_labels):
                area = stats[i, cv2.CC_STAT_AREA]
                comp_h = stats[i, cv2.CC_STAT_HEIGHT]
                comp_w = stats[i, cv2.CC_STAT_WIDTH]
                # Text characters: reasonable size, not too wide (not a line)
                aspect = comp_w / max(comp_h, 1)
                if (area >= min_area and area <= max_area and
                    comp_h >= min_h and comp_h <= max_h and aspect < 5):
                    clean[labels == i] = 255
                    kept += 1

            # Score: prefer results with 4-8 text components (6 characters expected)
            white_ratio = np.sum(clean > 0) / clean.size
            comp_score = 1.0 - abs(kept - 6) * 0.1  # Prefer ~6 components
            ratio_score = 1.0 - abs(white_ratio - 0.08) * 10  # Prefer ~8% white
            score = comp_score + ratio_score

            if score > best_score and white_ratio > 0.01:
                best_score = score
                best_img = clean
                print(f"[NOL-GPO] CAPTCHA method '{name}': kept={kept} components, white={white_ratio:.3f}, score={score:.2f}")

        if best_img is None or np.sum(best_img > 0) < best_img.size * 0.005:
            # Fallback: simple high threshold on gray
            _, best_img = cv2.threshold(gray, 160, 255, cv2.THRESH_BINARY)
            # Remove small noise
            kernel = np.ones((3, 3), np.uint8)
            best_img = cv2.morphologyEx(best_img, cv2.MORPH_OPEN, kernel, iterations=1)
            print("[NOL-GPO] CAPTCHA using fallback threshold")

        # Remove diagonal/horizontal noise lines using morphological operations
        # Lines are thin (~1-3px) and long; text strokes are thicker
        # Step 1: Detect lines with elongated kernels at various angles
        line_removed = best_img.copy()
        for angle in [0, 15, 30, 45, 60, 75, 90, 105, 120, 135, 150, 165]:
            length = max(w // 6, 30)  # line kernel length
            rad = np.deg2rad(angle)
            dx = int(np.cos(rad) * length / 2)
            dy = int(np.sin(rad) * length / 2)
            line_kernel = np.zeros((abs(dy) * 2 + 1, abs(dx) * 2 + 1), np.uint8)
            cv2.line(line_kernel, (abs(dx) - dx, abs(dy) - dy), (abs(dx) + dx, abs(dy) + dy), 1, 1)
            if line_kernel.sum() < 5:
                continue
            # Detect lines matching this angle
            detected = cv2.morphologyEx(best_img, cv2.MORPH_OPEN, line_kernel)
            # Remove detected lines (but only thin parts, not where text overlaps)
            line_removed = cv2.subtract(line_removed, detected)

        # If line removal was too aggressive (removed too much text), blend back
        orig_white = np.sum(best_img > 0)
        removed_white = np.sum(line_removed > 0)
        if removed_white > orig_white * 0.4:
            best_img = line_removed
        # else keep original if line removal destroyed too much

        # Slight dilation to connect broken strokes (from line removal)
        kernel_d = np.ones((2, 2), np.uint8)
        best_img = cv2.dilate(best_img, kernel_d, iterations=1)

        # Remove small remaining noise dots after dilation
        num_labels2, labels2, stats2, _ = cv2.connectedComponentsWithStats(best_img, connectivity=8)
        clean2 = np.zeros_like(best_img)
        for i in range(1, num_labels2):
            area = stats2[i, cv2.CC_STAT_AREA]
            comp_h2 = stats2[i, cv2.CC_STAT_HEIGHT]
            if area >= 15 * (scale * scale) and comp_h2 >= 4 * scale:
                clean2[labels2 == i] = 255
        if np.sum(clean2 > 0) > best_img.size * 0.005:
            best_img = clean2

        # Invert: black text on white background
        result_img = cv2.bitwise_not(best_img)

        # Add white border
        border = 15
        result_img = cv2.copyMakeBorder(result_img, border, border, border, border,
                                         cv2.BORDER_CONSTANT, value=255)

        _, result = cv2.imencode('.png', result_img)
        return result.tobytes()

    except Exception as e:
        print(f"[NOL-GPO] GPO CAPTCHA preprocess error: {e}")
        return _preprocess_captcha_image(img_bytes)


async def _nol_handle_captcha(tab, url, config_dict):
    """Handle Interpark CAPTCHA page (text-based CAPTCHA).
    URL: tickets.interpark.com/onestop/seat (with CAPTCHA overlay)
    CAPTCHA: image with distorted text (letters), case-insensitive input.

    Strategy:
      1. Try to find CAPTCHA image via src URL and fetch it with browser fetch()
      2. Fallback: use CDP Page.captureScreenshot to clip just the image area
      3. OCR with ddddocr
      4. Fill + submit
    """
    debug = util.create_debug_logger(config_dict)
    print("[NOL] Handling CAPTCHA...")

    try:
        # ---- Step 1: Locate the CAPTCHA image and get base64 data ----
        img_data = await tab.evaluate('''
            (async function() {
                // Strategy A: find <img> whose src contains captcha-related keywords
                const imgs = document.querySelectorAll('img');
                for (const img of imgs) {
                    const src = (img.src || '').toLowerCase();
                    const alt = (img.alt || '').toLowerCase();
                    const cls = (img.className || '').toLowerCase();
                    const id = (img.id || '').toLowerCase();
                    const isCaptcha = src.includes('captcha') || src.includes('captimg')
                        || src.includes('verify') || src.includes('code')
                        || alt.includes('captcha') || cls.includes('captcha')
                        || id.includes('captcha') || cls.includes('verify');
                    if (isCaptcha || (img.width >= 80 && img.width <= 400 && img.height >= 25 && img.height <= 120)) {
                        // Try canvas draw first
                        try {
                            const c = document.createElement('canvas');
                            c.width = img.naturalWidth || img.width;
                            c.height = img.naturalHeight || img.height;
                            const ctx = c.getContext('2d');
                            ctx.drawImage(img, 0, 0);
                            const data = c.toDataURL('image/png').split(',')[1];
                            if (data && data.length > 100) return data;
                        } catch(e) {}
                        // Canvas failed (cross-origin) — fetch the src directly
                        if (img.src) {
                            try {
                                const resp = await fetch(img.src, { credentials: 'include' });
                                const blob = await resp.blob();
                                return await new Promise(resolve => {
                                    const r = new FileReader();
                                    r.onloadend = () => resolve(r.result.split(',')[1]);
                                    r.readAsDataURL(blob);
                                });
                            } catch(e2) {}
                        }
                    }
                }

                // Strategy B: look for canvas elements (some CAPTCHAs render on canvas)
                const canvases = document.querySelectorAll('canvas');
                for (const canvas of canvases) {
                    if (canvas.width >= 80 && canvas.height >= 25) {
                        try {
                            const data = canvas.toDataURL('image/png').split(',')[1];
                            if (data && data.length > 100) return data;
                        } catch(e) {}
                    }
                }

                // Strategy C: find any img inside a captcha-like container
                const containers = document.querySelectorAll(
                    '[class*="captcha" i], [id*="captcha" i], [class*="verify" i], [class*="code" i]');
                for (const cont of containers) {
                    const img = cont.querySelector('img');
                    if (img && img.src) {
                        try {
                            const resp = await fetch(img.src, { credentials: 'include' });
                            const blob = await resp.blob();
                            return await new Promise(resolve => {
                                const r = new FileReader();
                                r.onloadend = () => resolve(r.result.split(',')[1]);
                                r.readAsDataURL(blob);
                            });
                        } catch(e) {}
                    }
                }

                // Return bounding rect info so we can use CDP screenshot as fallback
                for (const img of imgs) {
                    if (img.width >= 80 && img.width <= 400 && img.height >= 25 && img.height <= 120) {
                        const rect = img.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            return 'rect:' + JSON.stringify({
                                x: Math.round(rect.x), y: Math.round(rect.y),
                                w: Math.round(rect.width), h: Math.round(rect.height)
                            });
                        }
                    }
                }
                return null;
            })()
        ''', await_promise=True)

        if not img_data:
            # Debug: dump page info to help diagnose
            page_info = await tab.evaluate('''
                (function() {
                    const imgs = document.querySelectorAll('img');
                    const info = [];
                    imgs.forEach((img, i) => {
                        info.push({i: i, src: (img.src||'').substring(0,80),
                            w: img.width, h: img.height,
                            cls: (img.className||'').substring(0,40),
                            id: img.id||''});
                    });
                    const inputs = document.querySelectorAll('input');
                    const inputInfo = [];
                    inputs.forEach((inp, i) => {
                        inputInfo.push({i: i, type: inp.type,
                            placeholder: (inp.placeholder||'').substring(0,40),
                            name: inp.name||'', id: inp.id||''});
                    });
                    return JSON.stringify({imgs: info.slice(0,10), inputs: inputInfo.slice(0,10)});
                })()
            ''')
            debug.log(f"[NOL] CAPTCHA image not found. Page info: {page_info}")
            return False

        # Fallback: CDP clip screenshot if we only got bounding rect
        if isinstance(img_data, str) and img_data.startswith('rect:'):
            rect_info = json.loads(img_data[5:])
            debug.log(f"[NOL] Using CDP screenshot for CAPTCHA area: {rect_info}")
            try:
                result = await tab.send(cdp.page.capture_screenshot(
                    format_='png',
                    clip=cdp.page.Viewport(
                        x=rect_info['x'], y=rect_info['y'],
                        width=rect_info['w'], height=rect_info['h'],
                        scale=2
                    )
                ))
                img_data = result
                debug.log(f"[NOL] CDP screenshot captured ({len(img_data)} chars)")
            except Exception as e:
                debug.log(f"[NOL] CDP screenshot failed: {e}")
                return False

        # ---- Step 2: OCR the image ----
        ocr = _get_ocr()
        if ocr is None:
            debug.log("[NOL] ddddocr not available, manual CAPTCHA required")
            play_sound_while_ordering(config_dict)
            return False

        img_bytes = base64.b64decode(img_data)
        debug.log(f"[NOL] CAPTCHA image size: {len(img_bytes)} bytes")

        # Preprocess image to remove noise and improve OCR accuracy
        processed_bytes = _preprocess_captcha_image(img_bytes)
        debug.log(f"[NOL] Preprocessed image size: {len(processed_bytes)} bytes")

        # Try OCR on preprocessed image first, fallback to raw image
        ocr_answer = ocr.classification(processed_bytes)
        if not ocr_answer or len(re.sub(r'[^a-zA-Z0-9]', '', ocr_answer).strip()) < 3:
            debug.log(f"[NOL] Preprocessed OCR failed ({ocr_answer}), trying raw image...")
            ocr_answer = ocr.classification(img_bytes)

        if ocr_answer:
            # Clean up: remove spaces, keep alphanumeric
            ocr_answer = re.sub(r'[^a-zA-Z0-9]', '', ocr_answer).strip()
            print(f"[NOL] CAPTCHA OCR result: {ocr_answer}")
        else:
            debug.log("[NOL] CAPTCHA OCR returned empty")
            return False

        if len(ocr_answer) < 3:
            debug.log(f"[NOL] CAPTCHA answer too short ({ocr_answer}), retrying...")
            return False

        # ---- Step 3: Fill in the CAPTCHA answer ----
        cred_json = json.dumps({"v": ocr_answer})
        cred_safe = cred_json.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
        fill_result = await tab.evaluate('''
            (function() {
                const answer = JSON.parse(`''' + cred_safe + '''`).v;
                // Find the captcha input field — try multiple selectors
                let input = document.querySelector('input[placeholder*="captcha" i]');
                if (!input) input = document.querySelector('input[placeholder*="Enter the" i]');
                if (!input) input = document.querySelector('input[placeholder*="請輸入" i]');
                if (!input) input = document.querySelector('input[placeholder*="请输入" i]');
                if (!input) input = document.querySelector('input[placeholder*="입력" i]');
                if (!input) input = document.querySelector('input[placeholder*="验证" i]');
                if (!input) input = document.querySelector('input[placeholder*="인증" i]');
                if (!input) input = document.querySelector('input[placeholder*="code" i]');
                if (!input) {
                    // Any visible text input that's not a search box
                    const inputs = document.querySelectorAll('input[type="text"], input:not([type])');
                    for (const inp of inputs) {
                        if (inp.type !== 'hidden' && inp.offsetHeight > 0 &&
                            !inp.name.includes('search') && !inp.placeholder.toLowerCase().includes('search')) {
                            input = inp;
                            break;
                        }
                    }
                }
                if (input) {
                    input.focus();
                    input.click();
                    return 'found';
                }
                return 'input_not_found';
            })()
        ''')

        if fill_result == 'input_not_found':
            print("[NOL] CAPTCHA input not found")
            return False

        # Clear existing content + type answer via CDP (reliable for React)
        await asyncio.sleep(0.1)
        await tab.send(cdp.input_.dispatch_key_event(
            type_="keyDown", key="a", code="KeyA",
            windows_virtual_key_code=65, native_virtual_key_code=65, modifiers=2
        ))
        await tab.send(cdp.input_.dispatch_key_event(
            type_="keyUp", key="a", code="KeyA",
            windows_virtual_key_code=65, native_virtual_key_code=65, modifiers=2
        ))
        await asyncio.sleep(0.05)
        await tab.send(cdp.input_.dispatch_key_event(
            type_="keyDown", key="Backspace", code="Backspace",
            windows_virtual_key_code=8, native_virtual_key_code=8
        ))
        await tab.send(cdp.input_.dispatch_key_event(
            type_="keyUp", key="Backspace", code="Backspace",
            windows_virtual_key_code=8, native_virtual_key_code=8
        ))
        await asyncio.sleep(0.1)
        await tab.send(cdp.input_.insert_text(text=ocr_answer))
        fill_result = f'filled: {ocr_answer}'

        print(f"[NOL] CAPTCHA fill: {fill_result}")

        if fill_result and 'filled' in str(fill_result):
            await asyncio.sleep(0.3)

            # ---- Step 4: Submit CAPTCHA ----
            # Method 1: Press Enter (most reliable for form submission)
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="Enter", code="Enter",
                windows_virtual_key_code=13, native_virtual_key_code=13
            ))
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="Enter", code="Enter",
                windows_virtual_key_code=13, native_virtual_key_code=13
            ))
            print("[NOL] CAPTCHA: pressed Enter to submit")

            await asyncio.sleep(0.5)

            # Method 2: Also try clicking submit/confirm button as backup
            submit_result = await tab.evaluate('''
                (function() {
                    const btns = document.querySelectorAll('button, input[type="submit"], input[type="button"], a, div');
                    for (const btn of btns) {
                        const text = (btn.textContent || btn.value || '').trim();
                        const textLower = text.toLowerCase();
                        if (text === '完成輸入' || text === '完成输入' ||
                            textLower === 'submit' || text === '提交' || text === '확인' ||
                            textLower === 'confirm' || text === '确认' || text === '確認' ||
                            textLower === 'ok' || textLower === 'enter' || text === '입력' ||
                            text === '인증하기' || text === '인증' || text === '확인하기' ||
                            text === '다음' || textLower === 'next' || text === '下一步') {
                            btn.click();
                            return 'clicked: ' + text;
                        }
                    }
                    return 'no_button_found';
                })()
            ''')
            print(f"[NOL] CAPTCHA submit button: {submit_result}")

            await asyncio.sleep(1.5)

            # Check if CAPTCHA was wrong (still showing captcha elements)
            still_captcha = await tab.evaluate('''
                (function() {
                    const bodyText = document.body ? (document.body.innerText || '') : '';
                    if (bodyText.includes('請輸入畫面的文字') || bodyText.includes('请输入画面的文字')
                        || bodyText.includes('Enter the captcha')) return true;
                    const input = document.querySelector('input[placeholder*="captcha" i]')
                        || document.querySelector('input[placeholder*="Enter the" i]')
                        || document.querySelector('input[placeholder*="請輸入" i]')
                        || document.querySelector('input[placeholder*="请输入" i]');
                    return !!(input && input.offsetHeight > 0);
                })()
            ''')
            if still_captcha:
                print("[NOL] CAPTCHA may have been wrong, will retry")
                return False
            print("[NOL] CAPTCHA appears solved!")
            return True

        return False

    except Exception as e:
        debug.log(f"[NOL] CAPTCHA error: {e}")
        return False


async def _nol_handle_onestop_schedule(tab, url, config_dict):
    """Handle Interpark onestop schedule/date selection page.
    URL: tickets.interpark.com/onestop/schedule
    Page shows: calendar with selectable dates, time slots, and 'Next' button.
    """
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    debug.log("[NOL] On onestop schedule page (date/time selection)")

    date_keyword = config_dict.get("date_auto_select", {}).get("date_keyword", "")
    date_keywords = [k.strip() for k in date_keyword.split(";") if k.strip()] if date_keyword else []

    await asyncio.sleep(random.uniform(0.5, 1.0))

    try:
        # Check if a date is already selected (has blue circle)
        # If date keywords are provided, try to click the matching date
        if date_keywords:
            date_clicked = await tab.evaluate(f'''
                (function() {{
                    const keywords = {json.dumps(date_keywords)};
                    // Find all clickable date elements in the calendar
                    const dateEls = document.querySelectorAll('[class*="date"], [class*="day"], td, .calendar-day, [role="gridcell"]');
                    for (const el of dateEls) {{
                        const text = el.textContent.trim();
                        for (const kw of keywords) {{
                            if (text.includes(kw)) {{
                                el.click();
                                return 'clicked_date: ' + text;
                            }}
                        }}
                    }}
                    return 'no_match';
                }})()
            ''')
            if date_clicked and 'clicked_date' in str(date_clicked):
                debug.log(f"[NOL] Date selected by keyword: {date_clicked}")
                await asyncio.sleep(0.5)

        # Check if a time slot needs to be selected
        # From screenshot: "6:00 PM" is shown as a selectable time
        time_selected = await tab.evaluate('''
            (function() {
                // Look for time slot elements and click the first available one
                const timeEls = document.querySelectorAll('[class*="time"], [class*="session"], [class*="slot"]');
                for (const el of timeEls) {
                    const text = el.textContent.trim();
                    if (text.match(/\\d+:\\d+/)) {
                        // Check if not already selected
                        if (!el.classList.contains('selected') && !el.classList.contains('active')) {
                            el.click();
                            return 'clicked_time: ' + text;
                        }
                        return 'already_selected: ' + text;
                    }
                }
                return 'no_time_found';
            })()
        ''')
        if time_selected:
            debug.log(f"[NOL] Time slot: {time_selected}")

        await asyncio.sleep(0.3)

        # Click "Next" button
        next_clicked = await tab.evaluate('''
            (function() {
                const btns = document.querySelectorAll('button, a, input[type="submit"]');
                for (const btn of btns) {
                    const text = (btn.textContent || btn.value || '').trim();
                    if (text === 'Next' || text === '下一步' || text === '다음' ||
                        text === 'next' || text === 'NEXT' || text === '次へ') {
                        btn.click();
                        return 'clicked: ' + text;
                    }
                }
                return 'not_found';
            })()
        ''')

        if next_clicked and 'clicked' in str(next_clicked):
            debug.log(f"[NOL] Next button: {next_clicked}")
            await asyncio.sleep(1.0)
            play_sound_while_ordering(config_dict)
            return True
        else:
            debug.log("[NOL] Could not find Next button")
            return False

    except Exception as e:
        debug.log(f"[NOL] Onestop schedule error: {e}")
        return False


async def _nol_handle_onestop_seat(tab, url, config_dict):
    """Handle Interpark onestop seat selection page.
    URL: tickets.interpark.com/onestop/seat
    This page may show a CAPTCHA before seat selection.
    """
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    debug.log("[NOL] On onestop seat selection page")

    await asyncio.sleep(0.3)

    try:
        # Kill ALL beforeunload handlers to prevent Chrome's native "Leave site?" dialog
        try:
            await tab.evaluate('''
                (function() {
                    window.onbeforeunload = null;
                    // Override addEventListener to block any future beforeunload registrations
                    var origAdd = EventTarget.prototype.addEventListener;
                    EventTarget.prototype.addEventListener = function(type, fn, opts) {
                        if (type === 'beforeunload') return;
                        return origAdd.call(this, type, fn, opts);
                    };
                    // Remove existing beforeunload listeners by cloning the window proxy trick
                    // The most reliable way: just override the event entirely
                    Object.defineProperty(window, 'onbeforeunload', {
                        get: function() { return null; },
                        set: function() { },
                        configurable: true
                    });
                })()
            ''')
        except Exception:
            pass

        # Set up auto-accept for any native browser dialogs via CDP
        async def _auto_accept_dialog(event: cdp.page.JavascriptDialogOpening):
            print(f"[NOL] Auto-accepting browser dialog: {event.message[:50]}")
            try:
                await tab.send(cdp.page.handle_java_script_dialog(accept=True))
            except Exception:
                pass

        try:
            tab.add_handler(cdp.page.JavascriptDialogOpening, _auto_accept_dialog)
        except Exception:
            pass

        # Auto-accept any pending native browser dialog
        try:
            await tab.send(cdp.page.handle_java_script_dialog(accept=True))
        except Exception:
            pass

        # First: check if order confirmation dialog is showing and click "確認" to proceed
        confirm_dialog_result = await tab.evaluate('''
            (function() {
                window.onbeforeunload = null;
                const bodyText = document.body ? (document.body.innerText || '') : '';
                // Check for the order confirmation dialog
                if (bodyText.includes('確定要移動至訂購確認') || bodyText.includes('移動時會失去現在的訂購')) {
                    const btns = document.querySelectorAll('button');
                    for (const btn of btns) {
                        const text = btn.textContent.trim();
                        // Click "確認" (confirm), NOT "取消" (cancel)
                        if (text === '確認' || text === '确认') {
                            btn.click();
                            return 'confirmed_order: ' + text;
                        }
                    }
                    return 'dialog_found_no_confirm_btn';
                }
                // Dismiss other non-order dialogs (error popups, etc.)
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    const text = btn.textContent.trim();
                    if (text === '취소') {
                        btn.click();
                        return 'dismissed_other: ' + text;
                    }
                }
                return 'no_dialog';
            })()
        ''')
        if confirm_dialog_result and 'confirmed_order' in str(confirm_dialog_result):
            print(f"[NOL] ✅ Confirmed order dialog: {confirm_dialog_result}")
            try:
                await tab.send(cdp.page.handle_java_script_dialog(accept=True))
            except Exception:
                pass
            try:
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                print("[NOL] ✅ Page navigated after confirm")
            play_sound_while_ordering(config_dict)
            return True

        # Check if CAPTCHA is ACTIVELY present on this page
        has_captcha = await tab.evaluate('''
            (function() {
                const bodyText = document.body ? (document.body.innerText || '') : '';

                // Check for captcha-related visible text on page
                const hasCaptchaText = bodyText.includes('請輸入畫面的文字')
                    || bodyText.includes('请输入画面的文字')
                    || bodyText.includes('輸入文字後才能選擇座位')
                    || bodyText.includes('输入文字后才能选择座位')
                    || bodyText.includes('Enter the captcha')
                    || bodyText.includes('Case-insensitive')
                    || bodyText.includes('보안문자')
                    || bodyText.includes('验证码')
                    || bodyText.includes('驗證碼');
                if (hasCaptchaText) return true;

                // Check for captcha input field (multiple languages)
                const input = document.querySelector('input[placeholder*="captcha" i]')
                    || document.querySelector('input[placeholder*="Enter the" i]')
                    || document.querySelector('input[placeholder*="請輸入" i]')
                    || document.querySelector('input[placeholder*="请输入" i]')
                    || document.querySelector('input[placeholder*="입력" i]');
                if (input && input.offsetHeight > 0) return true;

                return false;
            })()
        ''')

        print(f"[NOL] CAPTCHA check: {has_captcha}")
        if has_captcha:
            print("[NOL] CAPTCHA detected on seat page!")
            # Try up to 5 times with different OCR attempts
            for attempt in range(5):
                captcha_solved = await _nol_handle_captcha(tab, url, config_dict)
                if captcha_solved:
                    print(f"[NOL] ✅ CAPTCHA solved on attempt {attempt + 1}!")
                    await asyncio.sleep(1.5)
                    return True
                print(f"[NOL] CAPTCHA attempt {attempt + 1}/5 failed, refreshing...")

                # Step A: Clear the input field using CDP (focus + select all + delete)
                await tab.evaluate('''
                    (function() {
                        const input = document.querySelector('input[placeholder*="請輸入" i]')
                            || document.querySelector('input[placeholder*="请输入" i]')
                            || document.querySelector('input[placeholder*="captcha" i]')
                            || document.querySelector('input[placeholder*="입력" i]');
                        if (input) { input.focus(); input.click(); }
                        else {
                            const inputs = document.querySelectorAll('input[type="text"], input:not([type])');
                            for (const inp of inputs) {
                                if (inp.offsetHeight > 0 && inp.type !== 'hidden') { inp.focus(); inp.click(); return; }
                            }
                        }
                    })()
                ''')
                await asyncio.sleep(0.1)
                # Select all + delete via CDP
                await tab.send(cdp.input_.dispatch_key_event(
                    type_="keyDown", key="a", code="KeyA",
                    windows_virtual_key_code=65, native_virtual_key_code=65, modifiers=2
                ))
                await tab.send(cdp.input_.dispatch_key_event(
                    type_="keyUp", key="a", code="KeyA",
                    windows_virtual_key_code=65, native_virtual_key_code=65, modifiers=2
                ))
                await asyncio.sleep(0.05)
                await tab.send(cdp.input_.dispatch_key_event(
                    type_="keyDown", key="Backspace", code="Backspace",
                    windows_virtual_key_code=8, native_virtual_key_code=8
                ))
                await tab.send(cdp.input_.dispatch_key_event(
                    type_="keyUp", key="Backspace", code="Backspace",
                    windows_virtual_key_code=8, native_virtual_key_code=8
                ))
                await asyncio.sleep(0.2)

                # Step B: Click the CAPTCHA refresh ↻ icon
                # IMPORTANT: exclude seatplan/zoom elements — those are seat map controls!
                refresh_result = await tab.evaluate('''
                    (function() {
                        // The ↻ refresh icon is inside the CAPTCHA gray area,
                        // NOT in the seat map. Look for small clickable elements
                        // near the CAPTCHA image that are NOT seat-related.
                        const allEls = document.querySelectorAll('button, a, div, span, svg, img, i');
                        for (const el of allEls) {
                            const cls = (el.className || '').toString().toLowerCase();
                            // SKIP seat map elements
                            if (cls.includes('seatplan') || cls.includes('zoom')) continue;

                            const text = (el.textContent || '').trim();
                            const title = (el.title || '').toLowerCase();
                            const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();

                            if (cls.includes('refresh') || cls.includes('reload') || cls.includes('retry') ||
                                cls.includes('renew') || cls.includes('captcharefresh') ||
                                title.includes('refresh') || title.includes('reload') ||
                                ariaLabel.includes('refresh') || ariaLabel.includes('reload') ||
                                text === '↻' || text === '⟳' || text === '🔄') {
                                el.click();
                                return 'clicked_refresh: ' + (text || cls).substring(0, 50);
                            }
                        }

                        // Fallback: find the refresh icon by position
                        // It's typically a small element in the top-right area of the CAPTCHA box
                        // Look for the CAPTCHA image first, then find nearby small clickable elements
                        const captchaImg = Array.from(document.querySelectorAll('img')).find(
                            img => img.width >= 80 && img.width <= 400 && img.height >= 25 && img.height <= 120
                        );
                        if (captchaImg) {
                            const imgRect = captchaImg.getBoundingClientRect();
                            const parent = captchaImg.closest('div') || captchaImg.parentElement;
                            if (parent) {
                                // Find small clickable elements in the same container
                                const els = parent.parentElement ?
                                    parent.parentElement.querySelectorAll('button, a, div, span, svg, img') :
                                    parent.querySelectorAll('button, a, div, span, svg, img');
                                for (const el of els) {
                                    if (el === captchaImg) continue;
                                    const w = el.offsetWidth || el.getBoundingClientRect().width;
                                    const h = el.offsetHeight || el.getBoundingClientRect().height;
                                    // Small element (likely an icon), not the captcha image itself
                                    if (w >= 15 && w <= 50 && h >= 15 && h <= 50) {
                                        const r = el.getBoundingClientRect();
                                        // Should be near the top of the captcha area (refresh is top-right)
                                        if (r.top < imgRect.bottom) {
                                            el.click();
                                            return 'clicked_icon_near_captcha: ' + el.tagName + ' ' + (el.className||'').toString().substring(0,30);
                                        }
                                    }
                                }
                            }
                            // Last resort: click the captcha image itself
                            captchaImg.click();
                            return 'clicked_captcha_img';
                        }
                        return 'no_refresh_found';
                    })()
                ''')
                print(f"[NOL] CAPTCHA refresh: {refresh_result}")
                await asyncio.sleep(2.0)  # Wait for new image to load

            print("[NOL] ❌ CAPTCHA not solved after 5 attempts, please solve manually")
            play_sound_while_ordering(config_dict)
            return False

        # No CAPTCHA - proceed with seat selection
        print("[NOL] No CAPTCHA, proceeding to seat selection...")
        area_keyword = config_dict.get("area_auto_select", {}).get("area_keyword", "")
        area_keywords = [k.strip() for k in area_keyword.split(";") if k.strip()] if area_keyword else []
        ticket_number = config_dict.get("ticket_number", 1)

        await asyncio.sleep(0.5)

        # Diagnose page structure for debugging
        page_info = await tab.evaluate('''
            (function() {
                const info = {};
                info.url = location.href;
                info.iframes = document.querySelectorAll('iframe').length;
                info.svgs = document.querySelectorAll('svg').length;
                info.canvases = document.querySelectorAll('canvas').length;
                // Count all circles (SVG seats are usually circles)
                info.circles = document.querySelectorAll('circle').length;
                // Count elements with seat-related styles
                const allEls = document.querySelectorAll('*');
                let purpleDots = 0, grayDots = 0, clickableDots = 0;
                const seatLike = [];
                for (const el of allEls) {
                    const style = window.getComputedStyle(el);
                    const w = el.offsetWidth || parseInt(style.width) || 0;
                    const h = el.offsetHeight || parseInt(style.height) || 0;
                    const bg = style.backgroundColor || '';
                    const fill = el.getAttribute('fill') || '';
                    const cls = (el.className || '').toString().toLowerCase();
                    const tag = el.tagName.toLowerCase();
                    // Small circle-like elements (5-20px) are likely seats
                    const isSmallDot = (w >= 5 && w <= 25 && h >= 5 && h <= 25);
                    const isCircle = tag === 'circle';
                    if (isSmallDot || isCircle) {
                        // Check if it looks purple/blue (available) vs gray (sold)
                        const isPurple = bg.includes('138') || bg.includes('purple') ||
                            bg.includes('rgb(1') || fill.includes('#') ||
                            cls.includes('available') || cls.includes('select');
                        if (isPurple) purpleDots++;
                        else grayDots++;
                    }
                }
                info.purpleDots = purpleDots;
                info.grayDots = grayDots;
                // Check for old-style iframe seat map
                const seatFrame = document.querySelector('#ifrmSeat, [name="ifrmSeat"], #ifrmSeatDetail, [name="ifrmSeatDetail"]');
                info.hasIframeSeatMap = !!seatFrame;
                // List buttons
                info.buttons = [];
                document.querySelectorAll('button').forEach(b => {
                    const t = b.textContent.trim();
                    if (t.length > 0 && t.length < 50) info.buttons.push(t);
                });
                return JSON.stringify(info);
            })()
        ''')
        print(f"[NOL] Seat page info: {page_info}")

        # ---- Strategy 1: Old Interpark iframe seat map ----
        iframe_seat_result = await tab.evaluate('''
            (function() {
                // Check for nested iframes: ifrmSeat -> ifrmSeatDetail
                const seatFrame = document.querySelector('#ifrmSeat, [name="ifrmSeat"]');
                if (seatFrame) return 'has_iframe';
                return null;
            })()
        ''')

        if iframe_seat_result == 'has_iframe':
            debug.log("[NOL] Old-style iframe seat map detected")
            # For iframe-based maps, we need to switch into the iframe via CDP
            # Find available seat images inside the iframe
            seat_result = await tab.evaluate('''
                (function() {
                    try {
                        const frame1 = document.querySelector('#ifrmSeat, [name="ifrmSeat"]');
                        if (!frame1 || !frame1.contentDocument) return 'iframe_no_access';
                        const frame2 = frame1.contentDocument.querySelector('#ifrmSeatDetail, [name="ifrmSeatDetail"]');
                        const doc = frame2 ? frame2.contentDocument : frame1.contentDocument;
                        if (!doc) return 'inner_iframe_no_access';
                        // Old Interpark: seats are img elements with onclick="SelectSeat(...)"
                        const seats = doc.querySelectorAll('img[onclick*="SelectSeat"]');
                        if (seats.length > 0) {
                            seats[0].click();
                            return 'iframe_clicked: ' + seats.length + ' seats';
                        }
                        // Try any clickable small images
                        const imgs = doc.querySelectorAll('img');
                        let clicked = 0;
                        for (const img of imgs) {
                            if (img.width >= 5 && img.width <= 30 && img.height >= 5 && img.height <= 30) {
                                img.click();
                                clicked++;
                                if (clicked >= 1) return 'iframe_img_clicked';
                            }
                        }
                        return 'iframe_no_seats: imgs=' + imgs.length;
                    } catch(e) {
                        return 'iframe_error: ' + e.message;
                    }
                })()
            ''')
            print(f"[NOL] Iframe seat: {seat_result}")
        else:
            # ---- Strategy 2: New React onestop seat map (SVG/DOM circles) ----
            print("[NOL] New-style React seat map")

            # Try clicking available (purple/colored) seat dots
            # Step 1: Find available seat coordinates via JS
            seat_coords = await tab.evaluate('''
                (function() {
                    const circles = document.querySelectorAll('svg circle, svg rect');
                    const available = [];
                    for (const c of circles) {
                        const fill = (c.getAttribute('fill') || '').toLowerCase();
                        const cls = (c.getAttribute('class') || '').toLowerCase();
                        const opacity = c.getAttribute('opacity') || '1';
                        const isGray = fill === '#ccc' || fill === '#ddd' || fill === '#eee' ||
                            fill === 'gray' || fill === '#e0e0e0' || fill === '#f0f0f0' ||
                            fill === 'white' || fill === '#fff' || fill === '#ffffff' ||
                            fill === 'none' || fill === 'transparent';
                        const isSold = cls.includes('sold') || cls.includes('disable') ||
                            cls.includes('unavail') || cls.includes('occupied');
                        if (!isGray && !isSold && parseFloat(opacity) > 0.3 && fill !== '') {
                            const rect = c.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                available.push({
                                    x: Math.round(rect.x + rect.width / 2),
                                    y: Math.round(rect.y + rect.height / 2),
                                    fill: fill
                                });
                            }
                        }
                    }
                    if (available.length > 0) {
                        const idx = Math.floor(Math.random() * Math.min(available.length, 5));
                        return JSON.stringify({count: available.length, idx: idx, x: available[idx].x, y: available[idx].y, fill: available[idx].fill});
                    }
                    return JSON.stringify({count: 0});
                })()
            ''')
            print(f"[NOL] Seat coordinates: {seat_coords}")

            try:
                coords = json.loads(seat_coords) if isinstance(seat_coords, str) else {}
            except Exception:
                coords = {}

            if coords.get('count', 0) > 0:
                # Step 2: Use CDP to perform a real mouse click at the seat coordinates
                sx, sy = coords['x'], coords['y']
                await tab.send(cdp.input_.dispatch_mouse_event(
                    type_="mousePressed", x=sx, y=sy, button=cdp.input_.MouseButton.LEFT,
                    click_count=1
                ))
                await tab.send(cdp.input_.dispatch_mouse_event(
                    type_="mouseReleased", x=sx, y=sy, button=cdp.input_.MouseButton.LEFT,
                    click_count=1
                ))
                seat_result = f"svg_circle: {coords['count']} available, CDP clicked #{coords['idx']} at ({sx},{sy}) fill={coords.get('fill','')}"
                print(f"[NOL] Seat click result: {seat_result}")
            else:
                seat_result = 'no_seats_found: circles=0'

            # Fallback methods if no SVG seats found
            if 'no_seats_found' in str(seat_result):
                seat_result = await tab.evaluate('''
                (function() {

                    // Method B: DOM elements that look like seats (small colored dots)
                    const allEls = document.querySelectorAll('div, span, a, button');
                    const seatDots = [];
                    for (const el of allEls) {
                        const w = el.offsetWidth;
                        const h = el.offsetHeight;
                        if (w >= 5 && w <= 25 && h >= 5 && h <= 25 && el.offsetParent) {
                            const style = window.getComputedStyle(el);
                            const bg = style.backgroundColor;
                            const radius = style.borderRadius;
                            // Check if it's a circle shape
                            const isRound = radius === '50%' || parseInt(radius) >= w/2;
                            if (!isRound) continue;
                            // Check color — skip gray/white
                            const isGray = bg === 'rgb(204, 204, 204)' || bg === 'rgb(238, 238, 238)' ||
                                bg === 'rgb(255, 255, 255)' || bg === 'rgba(0, 0, 0, 0)' ||
                                bg === 'rgb(224, 224, 224)' || bg === 'rgb(245, 245, 245)';
                            const cls = (el.className || '').toLowerCase();
                            const isSold = cls.includes('sold') || cls.includes('disable') ||
                                cls.includes('unavail') || cls.includes('occupied');
                            if (!isGray && !isSold && bg !== '') {
                                seatDots.push(el);
                            }
                        }
                    }
                    if (seatDots.length > 0) {
                        const idx = Math.floor(Math.random() * Math.min(seatDots.length, 5));
                        seatDots[idx].click();
                        return 'dom_dots: ' + seatDots.length + ' available, clicked #' + idx;
                    }

                    // Method C: Try class-name based selectors
                    const selectors = [
                        '[class*="seat"]:not([class*="sold"]):not([class*="disabled"]):not([class*="unavailable"])',
                        '[class*="available"]', '[class*="selectable"]', '[class*="bookable"]',
                        '[data-seat-status="available"]', '[data-status="available"]',
                        '[data-seat]', '[data-seat-id]',
                    ];
                    for (const sel of selectors) {
                        const seats = document.querySelectorAll(sel);
                        const clickable = [...seats].filter(s => s.offsetHeight > 0 && s.offsetHeight <= 30);
                        if (clickable.length > 0) {
                            clickable[0].click();
                            return 'selector: ' + sel + ' (' + clickable.length + ')';
                        }
                    }

                    return 'no_seats_found: fallback';
                })()
            ''')
            print(f"[NOL] Fallback seat click result: {seat_result}")

        # Wait for seat selection to register
        await asyncio.sleep(1.5)

        # Check if a seat was actually selected (look for selected/highlighted state)
        selected_info = await tab.evaluate('''
            (function() {
                // Check for selected seat indicators
                const selected = document.querySelectorAll('[class*="selected" i], [class*="active" i], [class*="chosen" i], [data-selected="true"]');
                // Also check for seat info popup/panel
                const seatInfo = document.querySelector('[class*="seat-info" i], [class*="seatInfo" i], [class*="seat_info" i], [class*="selected-seat" i]');
                const popup = document.querySelector('[class*="popup" i]:not([class*="seatplan" i]), [class*="modal" i], [class*="bottom-sheet" i], [class*="bottomSheet" i]');
                return JSON.stringify({
                    selectedCount: selected.length,
                    hasSeatInfo: !!seatInfo,
                    hasPopup: !!popup,
                    popupText: popup ? popup.textContent.substring(0, 100) : ''
                });
            })()
        ''')
        print(f"[NOL] Seat selection state: {selected_info}")

        # Only proceed if a seat was actually clicked successfully
        if 'no_seats_found' in str(seat_result):
            print("[NOL] ⚠️ 尚未選到座位，等待下次嘗試...")
            return True

        # Seat was clicked — proceed to confirm
        print(f"[NOL] ✅ 座位已點擊: {seat_result}")

        # Try to set ticket quantity
        await _nol_set_ticket_quantity(tab, ticket_number, debug)

        # Try clicking Next/Confirm button (訂購確認)
        next_result = await _nol_click_next_step(tab, debug)

        if next_result:
            # Wait for confirmation dialog to appear
            await asyncio.sleep(1.0)

            # Handle confirmation dialog: "確定要移動至訂購確認 / 取消嗎？"
            try:
                for attempt in range(5):
                    dialog_result = await tab.evaluate('''
                        (function() {
                            const bodyText = document.body ? (document.body.innerText || '') : '';
                            if (bodyText.includes('確定要移動至訂購確認') || bodyText.includes('移動時會失去現在的訂購')) {
                                const btns = document.querySelectorAll('button');
                                for (const btn of btns) {
                                    const text = btn.textContent.trim();
                                    if (text === '確認' || text === '确认') {
                                        btn.click();
                                        return 'confirmed: ' + text;
                                    }
                                }
                                return 'dialog_found_no_btn';
                            }
                            return 'no_dialog';
                        })()
                    ''')
                    print(f"[NOL] Confirmation dialog check #{attempt+1}: {dialog_result}")
                    if dialog_result and 'confirmed' in str(dialog_result):
                        print("[NOL] ✅ Order confirmed! Proceeding to checkout...")
                        await asyncio.sleep(2.0)
                        play_sound_while_ordering(config_dict)
                        break
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                print("[NOL] ✅ Page navigated (confirmed), proceeding...")
                play_sound_while_ordering(config_dict)
            except Exception as e:
                print(f"[NOL] Dialog handling note: {e}")
                play_sound_while_ordering(config_dict)
        else:
            # If confirm button not found/clicked, alert user
            if 'no_seats_found' in str(seat_result):
                print("[NOL] ⚠️ 無法自動選座，請手動點選紫色座位")
            else:
                print("[NOL] ⚠️ 座位已點選，但無法點擊訂購確認按鈕")
            play_sound_while_ordering(config_dict)

        return True

    except asyncio.CancelledError:
        print("[NOL] ✅ Page navigated during seat handling, proceeding...")
        play_sound_while_ordering(config_dict)
        return True
    except Exception as e:
        print(f"[NOL] Onestop seat error: {e}")
        return False


async def _nol_handle_checkout(tab, url, config_dict):
    """Handle checkout/order confirmation page. Do NOT auto-submit unless configured."""
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    debug.log("[NOL] On checkout page")

    # Fill contact info if configured
    contact = config_dict.get("contact", {})
    real_name = contact.get("real_name", "")
    phone = contact.get("phone", "")

    if real_name:
        try:
            name_input = await tab.query_selector('input[name*="name" i]')
            if not name_input:
                name_input = await tab.query_selector('input[placeholder*="name" i]')
            if not name_input:
                name_input = await tab.query_selector('input[placeholder*="姓名" i]')
            if name_input:
                await name_input.clear_input()
                await name_input.send_keys(real_name)
                debug.log("[NOL] Name filled")
        except Exception as e:
            debug.log(f"[NOL] Fill name error: {e}")

    if phone:
        try:
            phone_input = await tab.query_selector('input[name*="phone" i]')
            if not phone_input:
                phone_input = await tab.query_selector('input[type="tel"]')
            if not phone_input:
                phone_input = await tab.query_selector('input[placeholder*="电话" i]')
            if phone_input:
                await phone_input.clear_input()
                await phone_input.send_keys(phone)
                debug.log("[NOL] Phone filled")
        except Exception as e:
            debug.log(f"[NOL] Fill phone error: {e}")

    # Try to check agreement checkboxes
    try:
        await tab.evaluate('''
            const checkboxes = document.querySelectorAll('input[type="checkbox"]');
            checkboxes.forEach(cb => {
                if (!cb.checked) {
                    cb.click();
                }
            });
        ''')
        debug.log("[NOL] Agreement checkboxes checked")
    except Exception:
        pass

    # Play sound to alert user
    play_sound_while_ordering(config_dict)

    # Send notifications
    send_discord_notification(config_dict, "[NOL] Ticket found! Please complete payment.")
    send_telegram_notification(config_dict, "[NOL] Ticket found! Please complete payment.")

    # Do NOT auto-submit order - let user confirm manually
    debug.log("[NOL] ⚠️ Checkout page ready - waiting for manual confirmation")
    debug.log("[NOL] ⚠️ Please complete payment manually in the browser")

    return True


async def _nol_handle_gpo_captcha(tab, config_dict):
    """Handle CAPTCHA on old-style globalinterpark.com seat map page.
    The CAPTCHA dialog shows:
    - Image with distorted text
    - Input: "请输入防止不当订票的文字"
    - Buttons: "重新选择日期" (re-select date) / "输入完毕" (submit)
    """
    debug = util.create_debug_logger(config_dict)
    print("[NOL-GPO] Handling CAPTCHA...")

    try:
        # Step 1: Find CAPTCHA image and get base64 data
        # CAPTCHA is likely inside an iframe (ifrmSeat, ifrmSeatDetail, etc.)
        # CRITICAL: Do NOT use fetch(img.src) — it downloads a NEW captcha from server!
        # Use canvas.drawImage (same-origin) or CDP screenshot (cross-origin) instead.
        img_data = await tab.evaluate('''
            (async function() {
                // Calculate absolute position of an element inside a (possibly nested) iframe
                function getAbsoluteRect(el, iframeEl) {
                    const rect = el.getBoundingClientRect();
                    let offsetX = 0, offsetY = 0;
                    if (iframeEl) {
                        const ifrRect = iframeEl.getBoundingClientRect();
                        offsetX = ifrRect.x;
                        offsetY = ifrRect.y;
                    }
                    return {
                        x: Math.round(rect.x + offsetX),
                        y: Math.round(rect.y + offsetY),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height)
                    };
                }

                // Find captcha image in a document
                async function findCaptchaImg(doc, sourceName, iframeEl) {
                    if (!doc) return null;
                    const imgs = doc.querySelectorAll('img');
                    for (const img of imgs) {
                        const src = (img.src || '').toLowerCase();
                        const w = img.naturalWidth || img.width;
                        const h = img.naturalHeight || img.height;
                        // CAPTCHA images are typically 100-500px wide, 25-150px tall
                        if (w >= 80 && w <= 500 && h >= 25 && h <= 150) {
                            // Try canvas draw (captures actual displayed pixels — best method)
                            try {
                                const c = document.createElement('canvas');
                                c.width = img.naturalWidth || img.width;
                                c.height = img.naturalHeight || img.height;
                                const ctx = c.getContext('2d');
                                ctx.drawImage(img, 0, 0);
                                const data = c.toDataURL('image/png').split(',')[1];
                                if (data && data.length > 100) return {data: data, w: c.width, h: c.height, source: sourceName, method: 'canvas'};
                            } catch(e) {}
                            // Canvas failed (cross-origin) — use CDP screenshot (NOT fetch!)
                            // IMPORTANT: fetch(img.src) would download a NEW captcha from server,
                            // which is DIFFERENT from what's displayed. Must use CDP screenshot.
                            const absRect = getAbsoluteRect(img, iframeEl);
                            if (absRect.w > 0 && absRect.h > 0) {
                                return {rect: absRect, source: sourceName, method: 'cdp'};
                            }
                        }
                    }
                    return null;
                }

                // Search order: ifrmSeat first (where txtCaptcha is), then ifrmSeatDetail, then others
                const searchOrder = [];

                // Find ALL iframes and their elements
                const allIframes = document.querySelectorAll('iframe');
                const priorityIframes = [];  // seat-related iframes first
                const otherIframes = [];
                for (const ifr of allIframes) {
                    const ifrId = (ifr.id || ifr.name || '').toLowerCase();
                    if (ifrId.includes('seat')) {
                        priorityIframes.push(ifr);
                    } else {
                        otherIframes.push(ifr);
                    }
                }

                for (const ifr of [...priorityIframes, ...otherIframes]) {
                    try {
                        const ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                        const ifrName = ifr.id || ifr.name || 'iframe';
                        // Also search nested iframes (e.g., ifrmSeat inside ifrmSeatDetail)
                        const nestedIframes = ifrDoc.querySelectorAll('iframe');
                        for (const nested of nestedIframes) {
                            try {
                                const nestedDoc = nested.contentDocument || nested.contentWindow.document;
                                const nestedName = ifrName + '>' + (nested.id || nested.name || 'nested');
                                // For nested iframes, calculate cumulative offset
                                const nestedRect = nested.getBoundingClientRect();
                                const ifrRect = ifr.getBoundingClientRect();
                                const fakeIframeEl = {getBoundingClientRect: () => ({
                                    x: ifrRect.x + nestedRect.x,
                                    y: ifrRect.y + nestedRect.y
                                })};
                                const result = await findCaptchaImg(nestedDoc, nestedName, fakeIframeEl);
                                if (result) return JSON.stringify(result);
                            } catch(e) {}
                        }
                        searchOrder.push({doc: ifrDoc, name: ifrName, iframeEl: ifr});
                    } catch(e) {}
                }

                // Also search main document
                searchOrder.push({doc: document, name: 'main', iframeEl: null});

                for (const {doc, name, iframeEl} of searchOrder) {
                    const result = await findCaptchaImg(doc, name, iframeEl);
                    if (result) return JSON.stringify(result);
                }

                return null;
            })()
        ''', await_promise=True)

        if not img_data:
            print("[NOL-GPO] CAPTCHA image not found")
            return False

        img_info = json.loads(img_data) if isinstance(img_data, str) else {}

        # Handle CDP screenshot fallback
        if 'rect' in img_info:
            rect = img_info['rect']
            print(f"[NOL-GPO] Using CDP screenshot for CAPTCHA: {rect}")
            try:
                result = await tab.send(cdp.page.capture_screenshot(
                    format_='png',
                    clip=cdp.page.Viewport(
                        x=rect['x'], y=rect['y'],
                        width=rect['w'], height=rect['h'],
                        scale=2
                    )
                ))
                raw_b64 = result
            except Exception as e:
                print(f"[NOL-GPO] CDP screenshot failed: {e}")
                return False
        elif 'data' in img_info:
            raw_b64 = img_info['data']
            print(f"[NOL-GPO] CAPTCHA image captured ({img_info.get('w')}x{img_info.get('h')}) method={img_info.get('method','unknown')}")
        else:
            print(f"[NOL-GPO] Unexpected img_info: {img_data[:100]}")
            return False

        # Step 2: OCR — use cached ddddocr instances for speed
        # Try both default and beta models, pick the best 6-char alphanumeric result
        if ddddocr is None:
            print("[NOL-GPO] ddddocr not available, manual CAPTCHA required")
            play_sound_while_ordering(config_dict)
            return False

        # Cache OCR instances as function attributes for speed
        if not hasattr(_nol_handle_gpo_captcha, '_ocr_default'):
            try:
                _nol_handle_gpo_captcha._ocr_default = ddddocr.DdddOcr(show_ad=False)
                print("[NOL-GPO] OCR default model cached")
            except Exception as e:
                print(f"[NOL-GPO] Failed to init ddddocr default: {e}")
                _nol_handle_gpo_captcha._ocr_default = None
        if not hasattr(_nol_handle_gpo_captcha, '_ocr_beta'):
            try:
                _nol_handle_gpo_captcha._ocr_beta = ddddocr.DdddOcr(show_ad=False, beta=True)
                print("[NOL-GPO] OCR beta model cached")
            except Exception:
                _nol_handle_gpo_captcha._ocr_beta = None

        ocr_default = _nol_handle_gpo_captcha._ocr_default
        ocr_beta = _nol_handle_gpo_captcha._ocr_beta
        if ocr_default is None and ocr_beta is None:
            print("[NOL-GPO] No OCR model available")
            return False

        img_bytes = base64.b64decode(raw_b64)
        print(f"[NOL-GPO] CAPTCHA image: {len(img_bytes)} bytes")

        # Save CAPTCHA image for debugging
        try:
            import time as _time
            ts = int(_time.time())
            debug_path = f"/tmp/captcha_debug_{ts}.png"
            with open(debug_path, 'wb') as f:
                f.write(img_bytes)
            print(f"[NOL-GPO] CAPTCHA saved to: {debug_path}")
        except Exception:
            ts = 0

        # Preprocess specifically for GPO-style CAPTCHA (light text on dark green bg with noise lines)
        processed_bytes = _preprocess_gpo_captcha(img_bytes)

        # Save processed image for debugging
        try:
            debug_path2 = f"/tmp/captcha_processed_{ts}.png"
            with open(debug_path2, 'wb') as f:
                f.write(processed_bytes)
            print(f"[NOL-GPO] Processed CAPTCHA saved to: {debug_path2}")
        except Exception:
            pass

        # Multi-model OCR: try both models on both processed and raw images
        # Pick the best result (closest to 6 alphanumeric characters)
        def clean_ocr(text):
            if not text:
                return ''
            return re.sub(r'[^a-zA-Z0-9]', '', text).strip().upper()

        def score_result(text):
            """Score OCR result: prefer exactly 6 alphanumeric chars, all uppercase letters"""
            if not text:
                return -10
            s = 0
            s += len(text)  # longer is better (up to a point)
            s -= abs(len(text) - 6) * 3  # penalize deviation from 6 chars
            if len(text) >= 4 and len(text) <= 8:
                s += 5  # bonus for reasonable length
            return s

        candidates = []

        # Try processed image with both models
        if ocr_default:
            try:
                r = clean_ocr(ocr_default.classification(processed_bytes))
                if r:
                    candidates.append(('default+processed', r, score_result(r)))
            except Exception:
                pass

        if ocr_beta:
            try:
                r = clean_ocr(ocr_beta.classification(processed_bytes))
                if r:
                    candidates.append(('beta+processed', r, score_result(r)))
            except Exception:
                pass

        # Try raw image with both models (fallback)
        if ocr_default:
            try:
                r = clean_ocr(ocr_default.classification(img_bytes))
                if r:
                    candidates.append(('default+raw', r, score_result(r)))
            except Exception:
                pass

        if ocr_beta:
            try:
                r = clean_ocr(ocr_beta.classification(img_bytes))
                if r:
                    candidates.append(('beta+raw', r, score_result(r)))
            except Exception:
                pass

        # Consensus boost: if multiple models agree on the same result, boost its score
        result_counts = {}
        for name, text, score in candidates:
            result_counts[text] = result_counts.get(text, 0) + 1
        for i, (name, text, score) in enumerate(candidates):
            if result_counts[text] >= 2:
                candidates[i] = (name, text, score + 5)  # consensus bonus

        # Sort by score, pick the best
        candidates.sort(key=lambda x: x[2], reverse=True)
        print(f"[NOL-GPO] OCR candidates: {[(c[0], c[1], c[2]) for c in candidates]}")

        if not candidates:
            print("[NOL-GPO] CAPTCHA OCR returned no results")
            return False

        ocr_answer = candidates[0][1]
        consensus = result_counts.get(ocr_answer, 0)
        print(f"[NOL-GPO] CAPTCHA OCR best: {ocr_answer} (from {candidates[0][0]}, consensus={consensus})")

        if len(ocr_answer) < 3:
            print(f"[NOL-GPO] CAPTCHA answer too short ({ocr_answer})")
            return False

        # Step 3: Fill in the answer
        cred_json = json.dumps({"v": ocr_answer})
        cred_safe = cred_json.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
        fill_result = await tab.evaluate('''
            (function() {
                const answer = JSON.parse(`''' + cred_safe + '''`).v;
                // Find input — check all docs including iframes
                function findInput(doc) {
                    if (!doc) return null;
                    let input = doc.querySelector('input[placeholder*="防止" i]');
                    if (!input) input = doc.querySelector('input[placeholder*="请输入" i]');
                    if (!input) input = doc.querySelector('input[placeholder*="請輸入" i]');
                    if (!input) input = doc.querySelector('input[placeholder*="captcha" i]');
                    if (!input) input = doc.querySelector('input[placeholder*="입력" i]');
                    if (!input) {
                        const inputs = doc.querySelectorAll('input[type="text"], input:not([type])');
                        for (const inp of inputs) {
                            if (inp.type !== 'hidden' && inp.offsetHeight > 0 &&
                                !inp.name.includes('search')) {
                                input = inp;
                                break;
                            }
                        }
                    }
                    return input;
                }

                // Also search by name/id "txtCaptcha" directly
                function findCaptchaById(doc) {
                    if (!doc) return null;
                    let inp = doc.querySelector('#txtCaptcha');
                    if (!inp) inp = doc.querySelector('input[name="txtCaptcha"]');
                    if (!inp) inp = doc.querySelector('input[id*="captcha" i]');
                    if (!inp) inp = doc.querySelector('input[name*="captcha" i]');
                    return inp;
                }

                // Recursively search ALL iframes at ALL depths
                function searchAllFrames(doc, name, depth) {
                    if (!doc || depth > 5) return null;
                    // Try finding input in this doc
                    let input = findCaptchaById(doc);
                    if (input) return {input: input, frame: name + ' (byId)'};
                    input = findInput(doc);
                    if (input) return {input: input, frame: name};
                    // Recurse into child iframes
                    const iframes = doc.querySelectorAll('iframe');
                    for (const ifr of iframes) {
                        try {
                            const ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                            const result = searchAllFrames(ifrDoc, name + '>' + (ifr.id || ifr.name || 'ifr'), depth + 1);
                            if (result) return result;
                        } catch(e) {}
                    }
                    return null;
                }

                let input = null;
                let foundIn = '';
                const result = searchAllFrames(document, 'main', 0);
                if (result) {
                    input = result.input;
                    foundIn = result.frame;
                }

                if (input) {
                    input.focus();
                    input.click();
                    return 'found_in_' + foundIn;
                }
                return 'input_not_found';
            })()
        ''')

        print(f"[NOL-GPO] CAPTCHA input search: {fill_result}")

        # Even if input_not_found via focus method, continue with JS fill below
        # (txtCaptcha is in a 0x0 hidden iframe — focus won't work but .value= will)

        print(f"[NOL-GPO] CAPTCHA input: {fill_result}")

        # Fill via CDP keyboard (works if input is focused)
        await asyncio.sleep(0.1)
        try:
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="a", code="KeyA",
                windows_virtual_key_code=65, native_virtual_key_code=65, modifiers=2
            ))
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="a", code="KeyA",
                windows_virtual_key_code=65, native_virtual_key_code=65, modifiers=2
            ))
            await asyncio.sleep(0.05)
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key="Backspace", code="Backspace",
                windows_virtual_key_code=8, native_virtual_key_code=8
            ))
            await tab.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key="Backspace", code="Backspace",
                windows_virtual_key_code=8, native_virtual_key_code=8
            ))
            await asyncio.sleep(0.1)
            await tab.send(cdp.input_.insert_text(text=ocr_answer))
        except Exception as e:
            print(f"[NOL-GPO] CDP input failed: {e}")

        # Also set value directly via JS — specifically target txtCaptcha in ALL iframes
        cred_json2 = json.dumps({"v": ocr_answer})
        cred_safe2 = cred_json2.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
        js_fill_result = await tab.evaluate('''
            (function() {
                const answer = JSON.parse(`''' + cred_safe2 + '''`).v;
                const filled = [];

                function fillAllCaptchaInputs(doc, name, depth) {
                    if (!doc || depth > 5) return;
                    // Target txtCaptcha specifically
                    const byId = doc.querySelector('#txtCaptcha, input[name="txtCaptcha"]');
                    if (byId) {
                        byId.value = answer;
                        byId.dispatchEvent(new Event('input', {bubbles: true}));
                        byId.dispatchEvent(new Event('change', {bubbles: true}));
                        filled.push(name + ':txtCaptcha');
                    }
                    // Also fill any visible text input with captcha-like placeholder
                    const inputs = doc.querySelectorAll('input[type="text"], input:not([type])');
                    for (const inp of inputs) {
                        const ph = (inp.placeholder || '').toLowerCase();
                        const nm = (inp.name || '').toLowerCase();
                        if ((ph.includes('防止') || ph.includes('输入') || ph.includes('輸入') ||
                             ph.includes('captcha') || nm.includes('captcha')) && inp !== byId) {
                            inp.value = answer;
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                            filled.push(name + ':' + (inp.name || inp.id || 'input'));
                        }
                    }
                    // Recurse into child iframes
                    const iframes = doc.querySelectorAll('iframe');
                    for (const ifr of iframes) {
                        try {
                            const ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                            fillAllCaptchaInputs(ifrDoc, name + '>' + (ifr.id || ifr.name || 'ifr'), depth + 1);
                        } catch(e) {}
                    }
                }

                fillAllCaptchaInputs(document, 'main', 0);
                return filled.length > 0 ? 'filled: ' + filled.join(', ') : 'js_fill_failed';
            })()
        ''')
        print(f"[NOL-GPO] JS fill result: {js_fill_result}")
        print(f"[NOL-GPO] CAPTCHA filled: {ocr_answer}")

        await asyncio.sleep(0.3)

        # Step 4: Click "输入完毕" / "輸入完畢" button (search recursively)
        # Also try calling CAPTCHA validation JS functions directly
        submit_result = await tab.evaluate('''
            (function() {
                function findSubmitBtn(doc) {
                    if (!doc) return null;
                    const btns = doc.querySelectorAll('button, input[type="submit"], input[type="button"], a, div, span, td, img');
                    for (const btn of btns) {
                        const text = (btn.textContent || btn.value || btn.alt || '').trim();
                        if (text === '输入完毕' || text === '輸入完畢' || text === '입력완료' ||
                            text === 'Submit' || text === 'OK' || text === '确认' || text === '確認') {
                            // Log the button's onclick handler for debugging
                            const onclick = btn.getAttribute('onclick') || '';
                            const href = btn.getAttribute('href') || '';
                            const tag = btn.tagName;
                            const detail = 'tag=' + tag + ' onclick=' + onclick.substring(0, 60) + ' href=' + href.substring(0, 40);
                            btn.click();
                            return 'clicked: ' + text + ' [' + detail + ']';
                        }
                    }
                    return null;
                }

                // Recursive search through ALL iframes at ALL depths
                function searchFrames(doc, name, depth) {
                    if (!doc || depth > 5) return null;
                    const result = findSubmitBtn(doc);
                    if (result) return result + ' [' + name + ']';
                    const iframes = doc.querySelectorAll('iframe');
                    for (const ifr of iframes) {
                        try {
                            const ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                            const r = searchFrames(ifrDoc, name + '>' + (ifr.id || ifr.name || 'ifr'), depth + 1);
                            if (r) return r;
                        } catch(e) {}
                    }
                    return null;
                }

                return searchFrames(document, 'main', 0) || 'no_submit_btn';
            })()
        ''')
        print(f"[NOL-GPO] CAPTCHA submit: {submit_result}")

        # Also try calling CAPTCHA validation JS functions directly as backup
        try:
            js_validate = await tab.evaluate('''
                (function() {
                    // Try common CAPTCHA validation function names in all frames
                    function tryValidate(doc, name) {
                        if (!doc || !doc.defaultView) return null;
                        const win = doc.defaultView;
                        const fnNames = ['fnCaptchaOk', 'fnCaptchaCheck', 'fnCaptchaSubmit',
                            'fnConfirmCaptcha', 'CaptchaOk', 'CheckCaptcha', 'captchaCheck',
                            'fnSeatCaptchaOk', 'fnInputOk', 'fnOk', 'fnConfirm'];
                        for (const fn of fnNames) {
                            if (typeof win[fn] === 'function') {
                                return 'found_fn: ' + fn + ' in ' + name;
                            }
                        }
                        return null;
                    }
                    let result = tryValidate(document, 'main');
                    if (result) return result;
                    const iframes = document.querySelectorAll('iframe');
                    for (const ifr of iframes) {
                        try {
                            const ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                            result = tryValidate(ifrDoc, ifr.id || ifr.name || 'iframe');
                            if (result) return result;
                        } catch(e) {}
                    }
                    // Also dump all global function names containing 'captcha' or 'ok' or 'submit'
                    const mainFns = Object.keys(window).filter(k => {
                        const kl = k.toLowerCase();
                        return (typeof window[k] === 'function') &&
                            (kl.includes('captcha') || kl.includes('fnok') || kl.includes('fninput') ||
                             kl.includes('fnconfirm') || kl.includes('fncheck') || kl.includes('fnseat'));
                    });
                    return 'no_fn, main_fns=' + mainFns.slice(0, 10).join(',');
                })()
            ''')
            print(f"[NOL-GPO] CAPTCHA JS functions: {js_validate}")
        except Exception as e:
            print(f"[NOL-GPO] JS function scan error: {e}")

        await asyncio.sleep(0.8)

        # Check if CAPTCHA is still showing (wrong answer)
        # Use innerText keyword check — innerText excludes display:none elements,
        # so when CAPTCHA dialog is hidden after solving, keywords disappear.
        # This is more reliable than CSS visibility checks or DOM existence checks.
        still_captcha_info = await tab.evaluate('''
            (function() {
                var keywords = ['请输入文字', '請輸入文字', '请输入防止', '請輸入防止',
                                '输入完毕', '輸入完畢', '입력완료'];
                var keywordFound = false;
                var debugInfo = '';

                function checkText(doc, name, depth) {
                    if (!doc || !doc.body || depth > 8) return;
                    try {
                        var text = doc.body.innerText || '';
                        if (keywords.some(function(kw) { return text.includes(kw); })) {
                            keywordFound = true;
                            debugInfo += name + ':keyword_in_innerText ';
                        }
                    } catch(e) {}
                    try {
                        var iframes = doc.querySelectorAll('iframe');
                        for (var i = 0; i < iframes.length; i++) {
                            try {
                                var ifrDoc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                                checkText(ifrDoc, name + '>' + (iframes[i].id || iframes[i].name || 'ifr' + i), depth + 1);
                            } catch(e) {}
                        }
                    } catch(e) {}
                }
                checkText(document, 'main', 0);

                // Price page reached = definitely solved
                // Search main doc AND all iframes for PriceRow
                var priceReached = !!document.querySelector('[id^="PriceRow"]');
                if (!priceReached) {
                    var allIfrP = document.querySelectorAll('iframe');
                    for (var pi = 0; pi < allIfrP.length; pi++) {
                        try {
                            var piDoc = allIfrP[pi].contentDocument || allIfrP[pi].contentWindow.document;
                            if (piDoc.querySelector('[id^="PriceRow"]')) {
                                priceReached = true;
                                debugInfo += 'priceRow_in_iframe:' + (allIfrP[pi].id || allIfrP[pi].name) + ' ';
                                break;
                            }
                        } catch(e) {}
                    }
                }

                return JSON.stringify({
                    still: keywordFound && !priceReached,
                    keyword: keywordFound,
                    priceReached: priceReached,
                    debug: debugInfo
                });
            })()
        ''')
        try:
            captcha_state = json.loads(still_captcha_info) if isinstance(still_captcha_info, str) else {}
        except Exception:
            captcha_state = {}
        still_captcha = captcha_state.get('still', True)
        print(f"[NOL-GPO] Post-submit state: keyword={captcha_state.get('keyword')}, "
              f"priceReached={captcha_state.get('priceReached')}, "
              f"debug={captcha_state.get('debug','')}")
        if still_captcha:
            print("[NOL-GPO] CAPTCHA wrong, refreshing image...")
            # Click the refresh/reload button to get a new CAPTCHA image
            refresh_result = await tab.evaluate('''
                (function() {
                    function findRefreshBtn(doc, depth) {
                        if (!doc || depth > 5) return null;
                        // Look for refresh button: circular arrow icon, reload button
                        // Common patterns: img with refresh/reload in src, onclick with refresh/reload/new/change
                        const candidates = doc.querySelectorAll('img, a, button, div, span, input[type="image"]');
                        for (const el of candidates) {
                            const src = (el.src || el.getAttribute('src') || '').toLowerCase();
                            const onclick = (el.getAttribute('onclick') || '').toLowerCase();
                            const cls = (el.className || '').toLowerCase();
                            const alt = (el.alt || el.title || '').toLowerCase();
                            const text = (el.textContent || '').trim();

                            // Match refresh/reload patterns
                            if (src.includes('refresh') || src.includes('reload') || src.includes('btn_re') ||
                                onclick.includes('refresh') || onclick.includes('reload') || onclick.includes('newcaptcha') ||
                                onclick.includes('changecaptcha') || onclick.includes('captchaimg') ||
                                onclick.includes('fnreload') || onclick.includes('getimage') ||
                                alt.includes('refresh') || alt.includes('reload') || alt.includes('새로') ||
                                alt.includes('刷新') || alt.includes('重新') ||
                                cls.includes('refresh') || cls.includes('reload') ||
                                text === '🔄' || text === '↻') {
                                el.click();
                                return 'clicked_refresh: ' + (alt || src.substring(src.lastIndexOf('/')+1) || onclick.substring(0,30) || text).substring(0,40);
                            }
                        }
                        // Recurse into child iframes
                        const iframes = doc.querySelectorAll('iframe');
                        for (const ifr of iframes) {
                            try {
                                const ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                                const r = findRefreshBtn(ifrDoc, depth + 1);
                                if (r) return r + ' [' + (ifr.id || ifr.name) + ']';
                            } catch(e) {}
                        }
                        return null;
                    }
                    return findRefreshBtn(document, 0) || 'no_refresh_btn';
                })()
            ''')
            print(f"[NOL-GPO] Refresh: {refresh_result}")

            # If no refresh button found, try clicking the CAPTCHA image itself (common pattern)
            if 'no_refresh_btn' in str(refresh_result):
                await tab.evaluate('''
                    (function() {
                        function clickCaptchaImg(doc, depth) {
                            if (!doc || depth > 5) return false;
                            const imgs = doc.querySelectorAll('img');
                            for (const img of imgs) {
                                const w = img.naturalWidth || img.width;
                                const h = img.naturalHeight || img.height;
                                if (w >= 80 && w <= 500 && h >= 25 && h <= 150) {
                                    img.click();
                                    return true;
                                }
                            }
                            const iframes = doc.querySelectorAll('iframe');
                            for (const ifr of iframes) {
                                try {
                                    const ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                                    if (clickCaptchaImg(ifrDoc, depth + 1)) return true;
                                } catch(e) {}
                            }
                            return false;
                        }
                        clickCaptchaImg(document, 0);
                    })()
                ''')
                print("[NOL-GPO] Clicked CAPTCHA image to refresh")

            await asyncio.sleep(0.5)
            return False

        print("[NOL-GPO] CAPTCHA appears solved!")
        return True

    except Exception as e:
        print(f"[NOL-GPO] CAPTCHA error: {e}")
        return False


async def _nol_handle_gpo_booking(tab, url, config_dict):
    """Handle old-style globalinterpark.com booking page (BookMain.asp).
    Flow: Select Date → Seat Map → Price/Discount → Delivery
    """
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    print(f"[NOL-GPO] On booking page: {url}")

    try:
        # Override alert/close to prevent blocking
        await tab.evaluate('''
            window.alert = function(x) { console.log('alert:', x); };
            window.close = function(x) { console.log('close:', x); };
        ''')
    except Exception:
        pass

    # Track dialog messages for retry detection
    _gpo_last_dialog_msg = {'msg': '', 'time': 0}

    # Auto-accept any native alert dialog via CDP
    async def _auto_accept_gpo(event: cdp.page.JavascriptDialogOpening):
        msg = event.message[:120] if event.message else ''
        print(f"[NOL-GPO] Auto-accepting dialog: {msg[:60]}")
        _gpo_last_dialog_msg['msg'] = msg
        _gpo_last_dialog_msg['time'] = time.time()
        try:
            await tab.send(cdp.page.handle_java_script_dialog(accept=True))
        except Exception:
            pass

    try:
        tab.add_handler(cdp.page.JavascriptDialogOpening, _auto_accept_gpo)
    except Exception:
        pass

    # Dismiss any pending alert
    try:
        await tab.send(cdp.page.handle_java_script_dialog(accept=True))
    except Exception:
        pass

    await asyncio.sleep(0.1)

    try:
        # Detect which step we're on by checking actual page elements
        # IMPORTANT: Calendar and other content may be inside ifrmBookStep iframe
        step_info = await tab.evaluate('''
            (function() {
                const title = document.title || '';

                // Try to get ifrmBookStep iframe document
                let iframeDoc = null;
                const iframe = document.getElementById('ifrmBookStep');
                if (iframe) {
                    try {
                        iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                    } catch(e) {}
                }

                // Check for calendar in BOTH main doc and iframe
                let hasCalendar = false;
                function checkCalendar(doc) {
                    if (!doc) return false;
                    const tables = doc.querySelectorAll('table');
                    for (const t of tables) {
                        const text = t.textContent || '';
                        if (text.includes('Sun') || text.includes('Mon') || text.includes('日') ||
                            text.includes('一') || text.includes('二')) {
                            return true;
                        }
                    }
                    return false;
                }
                hasCalendar = checkCalendar(document) || checkCalendar(iframeDoc);

                // Check for ifrmBookStep existence (means we are on the booking page)
                const hasIframeBookStep = !!iframe;

                // Check for actual seat iframe (not step bar text)
                const seatIframe = document.querySelector('#ifrmSeatDetail, iframe[name*="seat" i]');
                let hasSeatMap = false;
                if (seatIframe) {
                    try {
                        const seatDoc = seatIframe.contentDocument || seatIframe.contentWindow.document;
                        // Only count as seat map if the iframe has actual content
                        const seatHtml = seatDoc && seatDoc.body ? seatDoc.body.innerHTML : '';
                        hasSeatMap = seatHtml.length > 100;
                    } catch(e) {
                        hasSeatMap = true; // Cross-origin means it has content
                    }
                }

                // Check for select dropdowns (time selection) in main doc AND iframe
                const selects = document.querySelectorAll('select');
                let selectCount = selects.length;
                if (iframeDoc) {
                    selectCount += iframeDoc.querySelectorAll('select').length;
                }

                // Check for Next buttons
                const nextBtn = document.querySelector('#LargeNextBtnImage, #NextStepImage, #SmallNextBtnImage');

                // Check for price/discount form elements (Step 3 of GPO flow)
                // PriceRow001 is the typical ID for the first price row with ticket count select
                // Search main doc AND all iframes (price form may be inside ifrmBookStep)
                let hasPriceForm = false;
                {
                    const ps = document.querySelector('#PriceRow001, [id^="PriceRow"], select[name*="price" i]');
                    if (ps) {
                        hasPriceForm = true;
                    } else {
                        // Search inside iframes
                        const allIframesP = document.querySelectorAll('iframe');
                        for (const ifr of allIframesP) {
                            try {
                                const ifrDocP = ifr.contentDocument || ifr.contentWindow.document;
                                if (ifrDocP.querySelector('#PriceRow001, [id^="PriceRow"], select[name*="price" i]')) {
                                    hasPriceForm = true;
                                    break;
                                }
                            } catch(e) {}
                        }
                    }
                }

                // Check for delivery/personal info form (Step 4 of GPO flow)
                // Look for delivery method select, personal info inputs, or payment form
                // Search main doc AND all iframes
                let hasDeliveryForm = false;
                let hasPersonalInfoForm = false;
                {
                    function checkDelivery(doc) {
                        const deliveryEl = doc.querySelector(
                            'input[name*="delivery" i], select[name*="delivery" i], ' +
                            'input[name*="Delivery" i], input[name*="receive" i], ' +
                            '[id*="delivery" i], [id*="Delivery" i]'
                        );
                        if (deliveryEl) hasDeliveryForm = true;
                        const nameInput = doc.querySelector(
                            'input[name*="Name" i], input[name*="name" i], ' +
                            'input[name*="UserNm" i], input[name*="BuyerNm" i]'
                        );
                        const phoneInput = doc.querySelector(
                            'input[name*="Phone" i], input[name*="phone" i], ' +
                            'input[name*="Hp" i], input[name*="Mobile" i]'
                        );
                        if (nameInput || phoneInput) hasPersonalInfoForm = true;
                    }
                    checkDelivery(document);
                    if (!hasDeliveryForm && !hasPersonalInfoForm) {
                        const allIframesD = document.querySelectorAll('iframe');
                        for (const ifr of allIframesD) {
                            try {
                                checkDelivery(ifr.contentDocument || ifr.contentWindow.document);
                            } catch(e) {}
                        }
                    }
                }

                // Check for CAPTCHA dialog (text-based CAPTCHA on seat map page)
                // CAPTCHA may be in main page, ifrmBookStep, OR ifrmSeatDetail
                let hasCaptcha = false;
                let hasSliderCaptcha = false;
                const captchaKeywords = ['请输入文字', '請輸入文字', '请输入防止', '請輸入防止',
                    '输入完毕', '輸入完畢', '입력완료', 'captcha'];
                const sliderKeywords = ['滑动滑块', '滑動滑塊', '拼图', '拼圖', '퍼즐',
                    '放心订票', '放心訂票', 'slide', 'puzzle'];
                function checkCaptchaText(doc) {
                    if (!doc || !doc.body) return false;
                    const text = doc.body.innerText || '';
                    return captchaKeywords.some(kw => text.includes(kw));
                }
                function checkSliderCaptcha(doc) {
                    if (!doc || !doc.body) return false;

                    // Check for VISIBLE slider/puzzle popup overlay
                    // The puzzle is inside a modal/overlay that may be hidden when not active
                    function isVisible(el) {
                        if (!el || !doc.defaultView) return false;
                        const style = doc.defaultView.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') return false;
                        if (parseFloat(style.opacity) < 0.1) return false;
                        const rect = el.getBoundingClientRect();
                        // Must have reasonable size (popup should be > 100px)
                        return rect.width > 100 && rect.height > 100;
                    }

                    // Look for visible puzzle/slider containers
                    const candidates = doc.querySelectorAll(
                        '[class*="puzzle" i], [class*="slider" i][class*="captcha" i], ' +
                        '[class*="captcha" i][class*="modal" i], [class*="captcha" i][class*="overlay" i], ' +
                        '[id*="puzzle" i], [id*="slider" i][id*="captcha" i]'
                    );
                    for (const el of candidates) {
                        if (isVisible(el)) return true;
                    }

                    // Also check: visible element containing slider keywords
                    const allDivs = doc.querySelectorAll('div, section, dialog');
                    for (const div of allDivs) {
                        const text = (div.textContent || '').trim();
                        if (text.length > 5 && text.length < 200 &&
                            sliderKeywords.some(kw => text.includes(kw))) {
                            if (isVisible(div)) {
                                // Make sure it's a container (not deeply nested text)
                                const rect = div.getBoundingClientRect();
                                if (rect.width > 200 && rect.height > 200) return true;
                            }
                        }
                    }

                    return false;
                }
                // Check main page
                hasCaptcha = checkCaptchaText(document);
                // Check ifrmBookStep
                if (!hasCaptcha) hasCaptcha = checkCaptchaText(iframeDoc);
                // Check ifrmSeatDetail (CAPTCHA is often inside seat map iframe!)
                if (!hasCaptcha && seatIframe) {
                    try {
                        const seatDoc = seatIframe.contentDocument || seatIframe.contentWindow.document;
                        hasCaptcha = checkCaptchaText(seatDoc);
                    } catch(e) {}
                }
                // Also check ALL iframes as fallback
                if (!hasCaptcha) {
                    const allIframes = document.querySelectorAll('iframe');
                    for (const ifr of allIframes) {
                        try {
                            const ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                            if (checkCaptchaText(ifrDoc)) {
                                hasCaptcha = true;
                                break;
                            }
                        } catch(e) {}
                    }
                }

                // Check for slider/puzzle CAPTCHA (different from text CAPTCHA)
                hasSliderCaptcha = checkSliderCaptcha(document);
                if (!hasSliderCaptcha) hasSliderCaptcha = checkSliderCaptcha(iframeDoc);
                if (!hasSliderCaptcha) {
                    const allIframes2 = document.querySelectorAll('iframe');
                    for (const ifr of allIframes2) {
                        try {
                            if (checkSliderCaptcha(ifr.contentDocument || ifr.contentWindow.document)) {
                                hasSliderCaptcha = true;
                                break;
                            }
                        } catch(e) {}
                    }
                }

                // Debug: check iframe content
                let iframeDebug = 'no_iframe';
                if (iframeDoc && iframeDoc.body) {
                    const bodyText = iframeDoc.body.textContent.substring(0, 200);
                    iframeDebug = 'len=' + iframeDoc.body.innerHTML.length + ' text=' + bodyText.substring(0, 80);
                }

                // Check for selected seats (共 N 座 选择成功)
                let selectedSeatCount = 0;
                let hasCompletionBtn = false;
                function findSelectedCount(doc) {
                    if (!doc || !doc.body) return 0;
                    const text = (doc.body.innerText || '') + ' ' + (doc.body.textContent || '');
                    // Pattern: 共 N 座
                    const m1 = text.match(/共\s*(\d+)\s*座/);
                    if (m1 && parseInt(m1[1]) > 0) return parseInt(m1[1]);
                    // Pattern: N seat(s) selected
                    const m2 = text.match(/(\d+)\s*seat/i);
                    if (m2 && parseInt(m2[1]) > 0) return parseInt(m2[1]);
                    // Pattern: seat grade table with seat data (座位等级 + number)
                    if (text.includes('座位等级') || text.includes('座位等級') || text.includes('좌석등급')) {
                        const rows = doc.querySelectorAll('tr');
                        for (const row of rows) {
                            const rt = row.textContent || '';
                            if ((rt.includes('坐席') || rt.includes('Standing') || rt.includes('좌석')) &&
                                rt.match(/\d+号|\d+석|\d+번|\d+楼|\d+층|\d+区|\d+구/)) {
                                return 1;
                            }
                        }
                    }
                    return 0;
                }
                function checkCompletionBtn(doc) {
                    if (!doc || !doc.body) return false;
                    const els = doc.querySelectorAll('a, button, img, div, input');
                    const btnTexts = ['seat selection completed', '选择完成', '選擇完成',
                        '座位选择完成', '좌석선택완료', '좌석 선택 완료', 'selection completed'];
                    for (const el of els) {
                        const t = (el.textContent || el.alt || el.value || '').trim().toLowerCase();
                        if (btnTexts.some(bt => t.includes(bt))) {
                            // Check visibility
                            const style = doc.defaultView ? doc.defaultView.getComputedStyle(el) : null;
                            if (!style || (style.display !== 'none' && style.visibility !== 'hidden')) {
                                return true;
                            }
                        }
                    }
                    return false;
                }
                // Check all accessible frames for selected seats
                selectedSeatCount = findSelectedCount(document);
                hasCompletionBtn = checkCompletionBtn(document);
                if (selectedSeatCount === 0 && iframeDoc) selectedSeatCount = findSelectedCount(iframeDoc);
                if (!hasCompletionBtn && iframeDoc) hasCompletionBtn = checkCompletionBtn(iframeDoc);
                if (selectedSeatCount === 0 || !hasCompletionBtn) {
                    const allIframes3 = document.querySelectorAll('iframe');
                    for (const ifr of allIframes3) {
                        try {
                            const ifrDoc3 = ifr.contentDocument || ifr.contentWindow.document;
                            if (selectedSeatCount === 0) selectedSeatCount = findSelectedCount(ifrDoc3);
                            if (!hasCompletionBtn) hasCompletionBtn = checkCompletionBtn(ifrDoc3);
                        } catch(e) {}
                    }
                }

                // If hasSeatMap is true, calendar detection is probably from step bar — override
                if (hasSeatMap) hasCalendar = false;

                return JSON.stringify({
                    hasCalendar: hasCalendar,
                    hasIframeBookStep: hasIframeBookStep,
                    hasSeatMap: hasSeatMap,
                    hasCaptcha: hasCaptcha,
                    hasSliderCaptcha: hasSliderCaptcha,
                    hasPriceForm: hasPriceForm,
                    hasDeliveryForm: hasDeliveryForm,
                    hasPersonalInfoForm: hasPersonalInfoForm,
                    selectedSeatCount: selectedSeatCount,
                    hasCompletionBtn: hasCompletionBtn,
                    selectCount: selectCount,
                    hasNextBtn: !!nextBtn,
                    nextBtnId: nextBtn ? nextBtn.id : '',
                    title: title.substring(0, 50),
                    iframeDebug: iframeDebug
                });
            })()
        ''')
        print(f"[NOL-GPO] Step info: {step_info}")

        try:
            info = json.loads(step_info) if isinstance(step_info, str) else {}
        except Exception:
            info = {}

        # ---- Error/Retry Detection ----
        # If a dialog was shown recently with error keywords, it means seat was lost
        # The dialog auto-accepted, and the page may have gone back to seat map
        recent_dialog = _gpo_last_dialog_msg.get('msg', '')
        dialog_age = time.time() - _gpo_last_dialog_msg.get('time', 0)
        if recent_dialog and dialog_age < 10:
            error_keywords = ['선택하신 좌석', '이미 선택', '已被', '已選', '已选',
                              'already selected', 'already taken', 'no longer available',
                              '다른 고객', '他人', '실패', '失敗', '失败', 'failed',
                              '시간 초과', '超時', '超时', 'timeout', 'expired']
            if any(kw in recent_dialog for kw in error_keywords):
                print(f"[NOL-GPO] ⚠️ Seat lost! Dialog: {recent_dialog[:60]}")
                print("[NOL-GPO] 🔄 Returning to seat map to retry...")
                _gpo_last_dialog_msg['msg'] = ''  # Clear to avoid re-triggering
                # The page should auto-return to seat map after dialog dismiss
                # If not, we'll detect seat map on next cycle
                await asyncio.sleep(1.5)
                return True

        # ---- Step 1: Select Date (calendar visible) ----
        if info.get('hasCalendar'):
            print("[NOL-GPO] On date selection step")

            # Dismiss any alert first
            try:
                await tab.send(cdp.page.handle_java_script_dialog(accept=True))
            except Exception:
                pass

            # Diagnostic: dump TDs with dates >= 14 to see colored ones (16=orange, 17=red)
            td_dump = await tab.evaluate('''
                (function() {
                    const iframe = document.getElementById('ifrmBookStep');
                    if (!iframe) return 'no_iframe';
                    let doc;
                    try {
                        doc = iframe.contentDocument || iframe.contentWindow.document;
                    } catch(e) { return 'crossorigin'; }
                    if (!doc) return 'no_doc';

                    const allTds = doc.querySelectorAll('td');
                    const dump = [];
                    for (let i = 0; i < allTds.length; i++) {
                        const td = allTds[i];
                        const text = td.textContent.trim();
                        // Extract leading number
                        const numMatch = text.match(/^(\\d{1,2})/);
                        if (!numMatch) continue;
                        const num = parseInt(numMatch[1]);
                        if (num < 14 || num > 20) continue;

                        const bg = doc.defaultView.getComputedStyle(td).backgroundColor;
                        const cls = td.className || '';
                        const style = td.getAttribute('style') || '';
                        const inner = td.innerHTML.substring(0, 120);
                        dump.push({i:i, date:num, text:text.substring(0,20), bg:bg, cls:cls, style:style, html:inner});
                    }
                    return JSON.stringify(dump);
                })()
            ''')
            print(f"[NOL-GPO] TD dump (dates 14-20): {td_dump}")

            # Calendar is inside ifrmBookStep iframe - access it
            # Strategy: find dates by BACKGROUND COLOR (orange=available, red=selected)
            # Don't rely on onclick attributes — events may be added via addEventListener
            # Support target date from config (date_auto_select.date_keyword)
            date_keyword = config_dict.get("date_auto_select", {}).get("date_keyword", "")
            # Extract target day numbers from keywords (e.g. "16" or "2026/5/16" or "5/16")
            target_days = []
            if date_keyword:
                for kw in date_keyword.split(";"):
                    kw = kw.strip()
                    if not kw:
                        continue
                    # Try to extract day number: "16", "5/16", "2026/5/16", "20260516"
                    import re as _re
                    # Match pure number like "16"
                    if _re.match(r'^\d{1,2}$', kw):
                        target_days.append(int(kw))
                    # Match "M/D" or "MM/DD"
                    elif m := _re.search(r'(\d{1,2})[/\-](\d{1,2})$', kw):
                        target_days.append(int(m.group(2)))
                    # Match "YYYYMMDD"
                    elif m := _re.match(r'\d{4}(\d{2})(\d{2})$', kw):
                        target_days.append(int(m.group(2)))
            target_days_json = json.dumps(target_days)
            print(f"[NOL-GPO] Target days from config: {target_days}")

            date_result = await tab.evaluate('''
                (function() {
                    const targetDays = ''' + target_days_json + ''';

                    // Get the iframe document
                    const iframe = document.getElementById('ifrmBookStep');
                    if (!iframe) return 'no_ifrmBookStep';

                    let doc;
                    try {
                        doc = iframe.contentDocument || iframe.contentWindow.document;
                    } catch(e) {
                        return 'iframe_crossorigin: ' + e.message;
                    }
                    if (!doc) return 'no_iframe_doc';

                    const allTds = doc.querySelectorAll('td');
                    const candidates = [];
                    const debugAll = [];

                    for (const td of allTds) {
                        const fullText = td.textContent.trim();

                        // Extract leading number: "16일 예매 불가능" → 16
                        const numMatch = fullText.match(/^(\\d{1,2})/);
                        if (!numMatch) continue;
                        const num = parseInt(numMatch[1]);
                        if (num < 1 || num > 31) continue;

                        // --- Detect availability via multiple signals ---
                        const bg = doc.defaultView.getComputedStyle(td).backgroundColor;
                        const cls = (td.className || '').toLowerCase();
                        const style = (td.getAttribute('style') || '').toLowerCase();
                        const link = td.querySelector('a');

                        // Parse RGB from computed background
                        const rgbMatch = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
                        let r = 0, g = 0, b = 0;
                        if (rgbMatch) {
                            r = parseInt(rgbMatch[1]);
                            g = parseInt(rgbMatch[2]);
                            b = parseInt(rgbMatch[3]);
                        }

                        // Signal 1: Text content says available/unavailable
                        // 가능 = possible/available, 불가능 = impossible/unavailable
                        const textLower = fullText;
                        const isTextAvailable = textLower.includes('가능') && !textLower.includes('불가능');
                        const isTextUnavailable = textLower.includes('불가능');

                        // Signal 2: Background color
                        const isOrange = rgbMatch && r > 200 && g > 100 && g < 200 && b < 80;
                        const isRed = rgbMatch && r > 200 && g < 80 && b < 80;
                        const isTransparent = bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent';

                        // Signal 3: CSS class hints
                        const clsAvail = cls.includes('on') || cls.includes('open') || cls.includes('avail') || cls.includes('active') || cls.includes('possible');
                        const clsUnavail = cls.includes('off') || cls.includes('dis') || cls.includes('sold') || cls.includes('closed');

                        // Signal 4: inline style background color
                        const styleOrange = style.includes('orange') || style.includes('#f9') || style.includes('#ff9') || style.includes('#e8') || style.includes('rgb(2');
                        const styleRed = style.includes('red') || style.includes('#f00') || style.includes('#ff0000') || style.includes('#c00');

                        // Collect debug
                        if (debugAll.length < 12) {
                            debugAll.push({d: num, avail: isTextAvailable, unavail: isTextUnavailable, bg: bg, cls: cls.substring(0,20), style: style.substring(0,30)});
                        }

                        // Determine if this date is bookable
                        const isAvailable = isTextAvailable || isOrange || clsAvail || styleOrange;
                        const isSelected = isRed || styleRed;
                        const isUnavailable = isTextUnavailable && !isAvailable;

                        if (isUnavailable && !isAvailable) continue; // Skip definitely unavailable

                        // Check if target day
                        const isTarget = targetDays.length > 0 && targetDays.includes(num);

                        // Priority system
                        let priority = 99;
                        if (targetDays.length > 0) {
                            if (isTarget && isAvailable) priority = 1;
                            else if (isTarget) priority = 2;
                            else if (isAvailable) priority = 3;
                            else if (isSelected) priority = 4;
                        } else {
                            if (isAvailable) priority = 1;
                            else if (isSelected) priority = 2;
                            else priority = 3;
                        }

                        candidates.push({td, link, num, bg, cls, priority, isAvailable, isSelected});
                    }

                    // Sort by priority
                    candidates.sort((a, b) => a.priority - b.priority);

                    // Debug output
                    const debugInfo = candidates.slice(0, 5).map(c => ({
                        d: c.num, p: c.priority, avail: c.isAvailable, sel: c.isSelected, bg: c.bg, cls: c.cls.substring(0,15)
                    }));

                    if (candidates.length > 0) {
                        const c = candidates[0];
                        // Click: try link inside td first, then td itself
                        if (c.link) {
                            c.link.click();
                        } else {
                            c.td.click();
                        }
                        return 'clicked: date=' + c.num + ' p=' + c.priority + ' avail=' + c.isAvailable + ' bg=' + c.bg + ' cls=' + c.cls + ' targetDays=' + JSON.stringify(targetDays) + ' candidates=' + JSON.stringify(debugInfo);
                    }

                    return 'no_available_date: tdCount=' + allTds.length + ' targetDays=' + JSON.stringify(targetDays) + ' debug=' + JSON.stringify(debugAll);
                })()
            ''')
            print(f"[NOL-GPO] Date click: {date_result}")

            if 'no_available_date' in str(date_result):
                print("[NOL-GPO] ⚠️ No available dates found — dumping debug info above")
                return True

            await asyncio.sleep(1.5)

            # Select time if dropdown exists (check BOTH main doc and iframe)
            time_result = await tab.evaluate('''
                (function() {
                    // Check main document first
                    function findAndSelectTime(doc) {
                        if (!doc) return null;
                        const selects = doc.querySelectorAll('select');
                        for (const sel of selects) {
                            if (sel.options.length > 1) {
                                // Select first non-empty option
                                for (let i = 0; i < sel.options.length; i++) {
                                    if (sel.options[i].value && sel.options[i].value !== '') {
                                        sel.selectedIndex = i;
                                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                                        return 'selected_time: ' + sel.options[i].text + ' (index=' + i + ')';
                                    }
                                }
                            }
                        }
                        return null;
                    }

                    // Try main document
                    let result = findAndSelectTime(document);
                    if (result) return result + ' [main]';

                    // Try ifrmBookStep iframe
                    const iframe = document.getElementById('ifrmBookStep');
                    if (iframe) {
                        try {
                            const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                            result = findAndSelectTime(iframeDoc);
                            if (result) return result + ' [iframe]';
                        } catch(e) {
                            return 'iframe_crossorigin: ' + e.message;
                        }
                    }

                    return 'no_time_select';
                })()
            ''')
            print(f"[NOL-GPO] Time: {time_result}")
            await asyncio.sleep(0.1)

            # Click "Next" button (check main doc - buttons are usually in main frame)
            next_result = await tab.evaluate('''
                (function() {
                    // Try specific Interpark button IDs in main doc
                    const btnIds = ['LargeNextBtnImage', 'NextStepImage', 'SmallNextBtnImage', 'btnNext'];
                    for (const id of btnIds) {
                        const btn = document.getElementById(id);
                        if (btn) {
                            btn.click();
                            return 'clicked: ' + id;
                        }
                    }

                    // Also check ifrmBookStep iframe for buttons
                    const iframe = document.getElementById('ifrmBookStep');
                    if (iframe) {
                        try {
                            const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                            for (const id of btnIds) {
                                const btn = iframeDoc.getElementById(id);
                                if (btn) {
                                    btn.click();
                                    return 'clicked_iframe: ' + id;
                                }
                            }
                        } catch(e) {}
                    }

                    // Try image buttons with "next" in src/alt
                    const imgs = document.querySelectorAll('img[onclick], input[type="image"]');
                    for (const img of imgs) {
                        const src = (img.src || '').toLowerCase();
                        const alt = (img.alt || '').toLowerCase();
                        if (src.includes('next') || alt.includes('next') || src.includes('btn_next')) {
                            img.click();
                            return 'clicked_img: ' + (alt || src.substring(src.lastIndexOf('/') + 1));
                        }
                    }
                    // Try text buttons
                    const btns = document.querySelectorAll('button, a, input[type="submit"]');
                    for (const btn of btns) {
                        const text = (btn.textContent || btn.value || '').trim().toLowerCase();
                        if (text.includes('next') || text === '下一步' || text === '다음') {
                            btn.click();
                            return 'clicked_text: ' + text;
                        }
                    }
                    // Try JS function call (reference bot uses fnNextStep('P'))
                    try {
                        if (typeof fnNextStep === 'function') {
                            fnNextStep('P');
                            return 'called_fnNextStep';
                        }
                    } catch(e) {}
                    return 'not_found';
                })()
            ''')
            print(f"[NOL-GPO] Next button: {next_result}")

            if 'clicked' in str(next_result) or 'called' in str(next_result):
                await asyncio.sleep(0.2)
                play_sound_while_ordering(config_dict)
            return True

        # ---- Step 2: Seat Map (with CAPTCHA handling) ----
        # IMPORTANT: hasPriceForm takes priority — on the Price page, hasSeatMap may still be
        # true because ifrmSeatDetail iframe exists in DOM (just hidden/empty).
        if (info.get('hasSeatMap') or info.get('hasCaptcha')) and not info.get('hasPriceForm'):
            print(f"[NOL-GPO] On seat map step (captcha={info.get('hasCaptcha')})")

            # Handle CAPTCHA if present
            if info.get('hasCaptcha'):
                # Check if CAPTCHA dialog is actually showing by looking for keywords
                # in innerText (NOT DOM existence or CSS visibility).
                # innerText only includes text from VISIBLE elements, so when the
                # CAPTCHA dialog is hidden (display:none), keywords disappear from innerText.
                # This avoids both problems:
                #   - CSS getComputedStyle/getBoundingClientRect fail in nested iframes
                #   - DOM existence persists even after dialog is hidden
                captcha_dialog_active = await tab.evaluate('''
                    (function() {
                        var keywords = ['请输入文字', '請輸入文字', '请输入防止', '請輸入防止',
                                        '输入完毕', '輸入完畢', '입력완료'];
                        function checkText(doc, depth) {
                            if (!doc || !doc.body || depth > 8) return false;
                            try {
                                var text = doc.body.innerText || '';
                                if (keywords.some(function(kw) { return text.includes(kw); })) return true;
                            } catch(e) {}
                            try {
                                var iframes = doc.querySelectorAll('iframe');
                                for (var i = 0; i < iframes.length; i++) {
                                    try {
                                        var ifrDoc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                                        if (checkText(ifrDoc, depth + 1)) return true;
                                    } catch(e) {}
                                }
                            } catch(e) {}
                            return false;
                        }
                        return checkText(document, 0);
                    })()
                ''')
                print(f"[NOL-GPO] CAPTCHA dialog active (keywords in innerText): {captcha_dialog_active}")
                if not captcha_dialog_active:
                    print("[NOL-GPO] CAPTCHA keywords not in visible text — already solved, skipping")
                    # Fall through to seat selection below (don't return True here)
                else:
                    print("[NOL-GPO] CAPTCHA input visible, solving (up to 15 attempts)...")
                    for attempt in range(15):
                        # Before each attempt, check if CAPTCHA is still present
                        if attempt > 0:
                            still_captcha = await tab.evaluate('''
                                (function() {
                                    // Price page reached = definitely solved (search main + iframes)
                                    if (document.querySelector('[id^="PriceRow"]')) return false;
                                    var allIfrPC = document.querySelectorAll('iframe');
                                    for (var pci = 0; pci < allIfrPC.length; pci++) {
                                        try {
                                            var pcDoc = allIfrPC[pci].contentDocument || allIfrPC[pci].contentWindow.document;
                                            if (pcDoc.querySelector('[id^="PriceRow"]')) return false;
                                        } catch(e) {}
                                    }
                                    // Check if CAPTCHA keywords still in visible text (innerText)
                                    // innerText excludes display:none elements, so hidden dialog = no keywords
                                    var keywords = ['请输入文字', '請輸入文字', '请输入防止', '請輸入防止',
                                                    '输入完毕', '輸入完畢', '입력완료'];
                                    function checkText(doc, depth) {
                                        if (!doc || !doc.body || depth > 8) return false;
                                        try {
                                            var text = doc.body.innerText || '';
                                            if (keywords.some(function(kw) { return text.includes(kw); })) return true;
                                        } catch(e) {}
                                        try {
                                            var iframes = doc.querySelectorAll('iframe');
                                            for (var i = 0; i < iframes.length; i++) {
                                                try {
                                                    var ifrDoc = iframes[i].contentDocument || iframes[i].contentWindow.document;
                                                    if (checkText(ifrDoc, depth + 1)) return true;
                                                } catch(e) {}
                                            }
                                        } catch(e) {}
                                        return false;
                                    }
                                    return checkText(document, 0);
                                })()
                            ''')
                            if not still_captcha:
                                print(f"[NOL-GPO] ✅ CAPTCHA no longer present (page advanced), breaking out")
                                await asyncio.sleep(0.2)
                                play_sound_while_ordering(config_dict)
                                break

                        try:
                            captcha_solved = await _nol_handle_gpo_captcha(tab, config_dict)
                            if captcha_solved:
                                print(f"[NOL-GPO] ✅ CAPTCHA solved on attempt {attempt + 1}!")
                                await asyncio.sleep(0.2)
                                play_sound_while_ordering(config_dict)
                                break
                            else:
                                print(f"[NOL-GPO] CAPTCHA attempt {attempt + 1}/15 failed")
                                await asyncio.sleep(0.3)
                        except Exception as e:
                            print(f"[NOL-GPO] CAPTCHA error: {e}")
                            await asyncio.sleep(0.3)
                    else:
                        print("[NOL-GPO] ❌ CAPTCHA not solved after 15 attempts")
                        play_sound_while_ordering(config_dict)
                    return True

            # Handle slider/puzzle CAPTCHA if present
            if info.get('hasSliderCaptcha') and not info.get('hasCaptcha'):
                print("[NOL-GPO] 🧩 Slider puzzle CAPTCHA detected!")
                # Slider puzzle CAPTCHA: drag a puzzle piece to the correct position
                # Strategy: Find the slider, get puzzle image, use edge detection to find gap position
                slider_result = await tab.evaluate('''
                    (function() {
                        // Find the slider puzzle container
                        function findSlider(doc, depth) {
                            if (!doc || !doc.body || depth > 5) return null;
                            const text = doc.body.innerText || '';

                            // Find the slider handle (the draggable button)
                            const handles = doc.querySelectorAll(
                                '[class*="slider" i] button, [class*="slider" i] [draggable], ' +
                                '[class*="handle" i], [class*="drag" i], ' +
                                'button[class*="arrow" i], [class*="slider-btn" i], ' +
                                '[class*="captcha" i] button'
                            );

                            // Find the puzzle image (canvas or img)
                            const puzzleImgs = doc.querySelectorAll(
                                'canvas, [class*="puzzle" i] img, [class*="captcha" i] img'
                            );

                            // Find slider track
                            const tracks = doc.querySelectorAll(
                                '[class*="slider" i][class*="track" i], [class*="slider" i][class*="bar" i], ' +
                                '[class*="slide-bar" i], [class*="drag-bar" i]'
                            );

                            const info = {
                                handles: handles.length,
                                puzzleImgs: puzzleImgs.length,
                                tracks: tracks.length,
                                frame: depth
                            };

                            // Get all elements info for debugging
                            const allEls = doc.querySelectorAll('*');
                            const classInfo = [];
                            for (const el of allEls) {
                                const cls = (el.className || '').toString().toLowerCase();
                                if (cls.includes('slider') || cls.includes('puzzle') || cls.includes('captcha') ||
                                    cls.includes('drag') || cls.includes('handle')) {
                                    classInfo.push({
                                        tag: el.tagName, cls: cls.substring(0, 50),
                                        rect: el.getBoundingClientRect ? JSON.stringify({
                                            x: Math.round(el.getBoundingClientRect().x),
                                            y: Math.round(el.getBoundingClientRect().y),
                                            w: Math.round(el.getBoundingClientRect().width),
                                            h: Math.round(el.getBoundingClientRect().height)
                                        }) : 'N/A'
                                    });
                                }
                            }
                            info.elements = classInfo.slice(0, 15);

                            // Recurse
                            const iframes = doc.querySelectorAll('iframe');
                            for (const ifr of iframes) {
                                try {
                                    const sub = findSlider(ifr.contentDocument || ifr.contentWindow.document, depth + 1);
                                    if (sub && sub.handles > 0) return sub;
                                } catch(e) {}
                            }

                            return info;
                        }
                        return JSON.stringify(findSlider(document, 0));
                    })()
                ''')
                print(f"[NOL-GPO] Slider info: {slider_result}")

                # For now, play sound to alert user — slider puzzle requires manual solving
                # (Automated slider solving requires complex image processing with edge detection)
                print("[NOL-GPO] ⚠️ 拼圖驗證碼需要手動滑動完成，請在瀏覽器中操作")
                play_sound_while_ordering(config_dict)
                await asyncio.sleep(3.0)
                return True

            # No CAPTCHA — handle seat/area selection on seat map
            area_keyword = config_dict.get("area_auto_select", {}).get("area_keyword", "")
            area_keywords = [k.strip() for k in area_keyword.split(";") if k.strip()] if area_keyword else []
            ticket_number = config_dict.get("ticket_number", 1)

            # Use selectedSeatCount and hasCompletionBtn from step detection
            seat_count = int(info.get('selectedSeatCount', 0))
            has_completion_btn = info.get('hasCompletionBtn', False)
            print(f"[NOL-GPO] Seat map: area_keywords={area_keywords}, tickets={ticket_number}, "
                  f"selectedSeats={seat_count}, completionBtn={has_completion_btn}")

            if seat_count > 0 or has_completion_btn:
                # Seats already selected — click "Seat selection completed"
                print(f"[NOL-GPO] ✅ {seat_count} seat(s) selected, clicking completion button...")
                next_result = await tab.evaluate('''
                    (function() {
                        // === PASS 1: Search by TEXT for "Seat selection completed" button ===
                        // Must come first — text match is most reliable
                        const textKeywords = ['Seat selection completed', '选择完成', '選擇完成',
                            '座位选择完成', '좌석선택완료', '좌석 선택 완료', 'selection completed'];

                        function findBtnByText(doc, depth) {
                            if (!doc || depth > 5) return null;
                            const els = doc.querySelectorAll('a, button, img, div, input, span');
                            for (const el of els) {
                                const text = (el.textContent || el.alt || el.value || '').trim();
                                for (const kw of textKeywords) {
                                    if (text.includes(kw)) {
                                        // Check visibility
                                        const style = doc.defaultView ? doc.defaultView.getComputedStyle(el) : null;
                                        if (style && (style.display === 'none' || style.visibility === 'hidden')) continue;
                                        el.click();
                                        return 'clicked_text: ' + text.substring(0, 50);
                                    }
                                }
                            }
                            const iframes = doc.querySelectorAll('iframe');
                            for (const ifr of iframes) {
                                try {
                                    const r = findBtnByText(ifr.contentDocument || ifr.contentWindow.document, depth+1);
                                    if (r) return r;
                                } catch(e) {}
                            }
                            return null;
                        }
                        const textResult = findBtnByText(document, 0);
                        if (textResult) return textResult;

                        // === PASS 2: Search by ID/src (fallback) ===
                        function findBtnById(doc, depth) {
                            if (!doc || depth > 5) return null;
                            const els = doc.querySelectorAll('a, button, img, div, input');
                            for (const el of els) {
                                const id = (el.id || '').toLowerCase();
                                const src = (el.src || el.getAttribute('src') || '').toLowerCase();
                                const onclick = (el.getAttribute('onclick') || '').toLowerCase();
                                if (id.includes('nextstep') || id.includes('smallnext') ||
                                    src.includes('nextstep') || src.includes('next_step') || src.includes('smallnext')) {
                                    // Visibility check
                                    const style = doc.defaultView ? doc.defaultView.getComputedStyle(el) : null;
                                    const rect = el.getBoundingClientRect();
                                    if (style && style.display !== 'none' && style.visibility !== 'hidden' &&
                                        rect.width > 10 && rect.height > 10) {
                                        el.click();
                                        return 'clicked_id: ' + (el.id || src.split('/').pop()).substring(0, 40);
                                    }
                                }
                                if (onclick.includes('fnnextstep') || onclick.includes('fnsubmit')) {
                                    el.click();
                                    return 'clicked_onclick: ' + onclick.substring(0, 40);
                                }
                            }
                            const iframes = doc.querySelectorAll('iframe');
                            for (const ifr of iframes) {
                                try {
                                    const r = findBtnById(ifr.contentDocument || ifr.contentWindow.document, depth+1);
                                    if (r) return r;
                                } catch(e) {}
                            }
                            return null;
                        }
                        const idResult = findBtnById(document, 0);
                        if (idResult) return idResult;

                        // === PASS 3: Call fnNextStep('S') directly ===
                        try {
                            if (typeof fnNextStep === 'function') {
                                fnNextStep('S');
                                return 'called_fnNextStep_S';
                            }
                        } catch(e) {}

                        // === PASS 4: Find red background button ===
                        function findRedBtn(doc, depth) {
                            if (!doc || !doc.body || depth > 5) return null;
                            const els = doc.querySelectorAll('a, div, button');
                            for (const el of els) {
                                const style = doc.defaultView ? doc.defaultView.getComputedStyle(el) : null;
                                if (!style) continue;
                                const bg = style.backgroundColor;
                                const rgbMatch = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
                                if (rgbMatch) {
                                    const r = parseInt(rgbMatch[1]), g = parseInt(rgbMatch[2]), b = parseInt(rgbMatch[3]);
                                    if (r > 150 && g < 80 && b < 80) {
                                        const text = (el.textContent || '').trim();
                                        if (text.length > 2 && text.length < 60) {
                                            el.click();
                                            return 'clicked_red_btn: ' + text.substring(0, 30);
                                        }
                                    }
                                }
                            }
                            const iframes = doc.querySelectorAll('iframe');
                            for (const ifr of iframes) {
                                try {
                                    const r = findRedBtn(ifr.contentDocument || ifr.contentWindow.document, depth+1);
                                    if (r) return r;
                                } catch(e) {}
                            }
                            return null;
                        }
                        const redResult = findRedBtn(document, 0);
                        if (redResult) return redResult;

                        return 'no_next_btn';
                    })()
                ''')
                print(f"[NOL-GPO] Next step: {next_result}")
                await asyncio.sleep(0.3)
                play_sound_while_ordering(config_dict)
                return True

            # ============================================================
            # Universal seat/area selection for GPO booking
            # Phase 1: Diagnostic — scan structure of the seat map page
            # Phase 2: Select price grade from right sidebar
            # Phase 3: Click area/block on seat map
            # Phase 4: Click individual seats within the area
            # ============================================================

            area_keywords_json = json.dumps(area_keywords)
            ticket_number_val = int(ticket_number) if ticket_number else 1

            # Phase 1: Diagnostic scan — understand page structure
            diag_result = await tab.evaluate('''
                (function() {
                    const result = {grades: [], areas: [], seats: [], iframes: []};

                    // Blacklist: onclick handlers that are NOT seat/area related
                    const onclickBlacklist = [
                        'cancelnoticeview', 'fnclose', 'close(', 'window.close',
                        'alert(', 'confirm(', 'fncapcharefresh', 'captcha',
                        'fncancelnotice', 'history.back', 'goback'
                    ];

                    function isBlacklisted(onclick) {
                        const lower = (onclick || '').toLowerCase();
                        return onclickBlacklist.some(b => lower.includes(b));
                    }

                    function scanDoc(doc, frameName, depth) {
                        if (!doc || !doc.body || depth > 6) return;
                        result.iframes.push({name: frameName, bodyLen: doc.body.innerHTML.length});

                        // Find price/grade selectors (right sidebar)
                        // These are typically radio buttons, select options, or clickable divs with price info
                        const gradeEls = doc.querySelectorAll(
                            'input[name*="Grade" i], input[name*="grade" i], ' +
                            'input[name*="price" i], select[name*="Grade" i], ' +
                            '[class*="grade" i], [class*="price-item" i], ' +
                            '[id*="Grade" i], [id*="price" i], ' +
                            'label[for*="grade" i], label[for*="Grade" i]'
                        );
                        for (const el of gradeEls) {
                            const text = (el.textContent || el.value || el.alt || '').trim().substring(0, 60);
                            const id = el.id || el.name || '';
                            const tag = el.tagName;
                            const checked = el.checked ? true : false;
                            result.grades.push({text, id, tag, checked, frame: frameName});
                        }

                        // Also find grade info from table rows or list items with price patterns
                        const allEls = doc.querySelectorAll('tr, li, div, span, td');
                        for (const el of allEls) {
                            const text = (el.textContent || '').trim();
                            // Match patterns like "Standing 154,000" or "坐席R 154,000원"
                            if (text.match(/[\d,]+\s*(원|₩|won|KRW)/i) && text.length < 100) {
                                const onclick = el.getAttribute('onclick') || '';
                                if (onclick && !isBlacklisted(onclick)) {
                                    result.grades.push({
                                        text: text.substring(0, 60), onclick: onclick.substring(0, 60),
                                        tag: el.tagName, frame: frameName
                                    });
                                }
                            }
                        }

                        // Find <area> elements (image map hotspots for venue blocks)
                        const areaEls = doc.querySelectorAll('area');
                        for (const el of areaEls) {
                            const alt = (el.alt || el.title || '').trim();
                            const href = (el.getAttribute('href') || '').substring(0, 80);
                            const onclick = (el.getAttribute('onclick') || '').substring(0, 80);
                            const shape = el.getAttribute('shape') || '';
                            if (!isBlacklisted(onclick) && !isBlacklisted(href)) {
                                result.areas.push({type: 'area', alt, href, onclick, shape, frame: frameName});
                            }
                        }

                        // Find onclick elements that look like seat/area selectors
                        const onclickEls = doc.querySelectorAll('[onclick]');
                        for (const el of onclickEls) {
                            const onclick = (el.getAttribute('onclick') || '');
                            const onclickLower = onclick.toLowerCase();
                            if (isBlacklisted(onclick)) continue;

                            // Only include seat/block/grade related onclick handlers
                            if (onclickLower.includes('selectblock') || onclickLower.includes('selectseat') ||
                                onclickLower.includes('selectgrade') || onclickLower.includes('selectarea') ||
                                onclickLower.includes('fnblock') || onclickLower.includes('fnseat') ||
                                onclickLower.includes('fnselect') || onclickLower.includes('fngrade') ||
                                onclickLower.includes('fnzone') || onclickLower.includes('fnarea') ||
                                onclickLower.includes('viewseat') || onclickLower.includes('seatview') ||
                                onclickLower.includes('openseat') || onclickLower.includes('clickblock') ||
                                onclickLower.includes('clickseat') || onclickLower.includes('clickarea') ||
                                onclickLower.includes('gradeclick') || onclickLower.includes('blockclick')) {
                                const text = (el.textContent || el.alt || el.title || '').trim().substring(0, 60);
                                const id = el.id || '';
                                result.areas.push({
                                    type: 'onclick', text, id, onclick: onclick.substring(0, 80),
                                    tag: el.tagName, frame: frameName
                                });
                            }
                        }

                        // Find individual seat elements (with SelectSeat pattern)
                        const seatOnclickEls = doc.querySelectorAll('[onclick*="SelectSeat"], [onclick*="selectSeat"], [onclick*="selectseat"]');
                        for (const el of seatOnclickEls) {
                            const onclick = (el.getAttribute('onclick') || '').substring(0, 80);
                            const id = el.id || '';
                            result.seats.push({id, onclick, tag: el.tagName, frame: frameName});
                        }

                        // Also find seats by common patterns: colored td/div with IDs
                        if (result.seats.length === 0) {
                            const coloredEls = doc.querySelectorAll('td[bgcolor], td[style*="background"], img[onclick], div[onclick]');
                            for (const el of coloredEls) {
                                const onclick = (el.getAttribute('onclick') || '');
                                if (isBlacklisted(onclick)) continue;
                                if (onclick && (onclick.toLowerCase().includes('seat') || onclick.toLowerCase().includes('select'))) {
                                    const id = el.id || '';
                                    result.seats.push({id, onclick: onclick.substring(0, 80), tag: el.tagName, frame: frameName});
                                }
                            }
                        }

                        // Recurse into iframes
                        const iframes = doc.querySelectorAll('iframe');
                        for (const ifr of iframes) {
                            try {
                                const ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                                const ifrName = frameName + '>' + (ifr.id || ifr.name || 'ifr');
                                scanDoc(ifrDoc, ifrName, depth + 1);
                            } catch(e) {}
                        }
                    }

                    scanDoc(document, 'main', 0);
                    return JSON.stringify(result);
                })()
            ''')

            try:
                diag = json.loads(diag_result) if isinstance(diag_result, str) else {}
            except Exception:
                diag = {}

            print(f"[NOL-GPO] Diag: grades={len(diag.get('grades',[]))}, areas={len(diag.get('areas',[]))}, "
                  f"seats={len(diag.get('seats',[]))}, iframes={[f.get('name','') for f in diag.get('iframes',[])]}")
            if diag.get('grades'):
                for g in diag['grades'][:5]:
                    print(f"  Grade: {g}")
            if diag.get('areas'):
                for a in diag['areas'][:8]:
                    print(f"  Area: {a}")
            if diag.get('seats'):
                print(f"  Seats found: {len(diag['seats'])} (first 3: {diag['seats'][:3]})")

            # Phase 2: If individual seats are already visible (SelectSeat pattern),
            # skip area selection and go directly to seat clicking
            has_individual_seats = len(diag.get('seats', [])) > 0
            has_area_map = len(diag.get('areas', [])) > 0

            if has_individual_seats:
                print(f"[NOL-GPO] Phase 4: Individual seats visible ({len(diag['seats'])} seats), clicking...")
                seat_click_result = await tab.evaluate('''
                    (function() {
                        const keywords = ''' + area_keywords_json + ''';
                        const ticketNum = ''' + str(ticket_number_val) + ''';
                        let clickedCount = 0;
                        const clickedSeats = [];

                        function findAndClickSeats(doc, depth) {
                            if (!doc || depth > 6) return;

                            // Find all elements with SelectSeat onclick
                            const seatEls = doc.querySelectorAll('[onclick*="SelectSeat"], [onclick*="selectSeat"], [onclick*="selectseat"]');
                            const availableSeats = [];

                            for (const el of seatEls) {
                                // Check if seat is visible and not already selected
                                const style = doc.defaultView ? doc.defaultView.getComputedStyle(el) : null;
                                if (style && style.display === 'none') continue;

                                const onclick = el.getAttribute('onclick') || '';
                                const id = el.id || '';
                                const bgcolor = el.getAttribute('bgcolor') || '';
                                const bgStyle = style ? style.backgroundColor : '';

                                // Parse SelectSeat parameters: SelectSeat('me','SeatGrade','Floor','RowNo','SeatNo','Block')
                                const params = onclick.match(/SelectSeat\\s*\\(([^)]+)\\)/i);
                                let seatInfo = {};
                                if (params) {
                                    const parts = params[1].replace(/'/g, '').split(',').map(s => s.trim());
                                    seatInfo = {
                                        me: parts[0] || '', grade: parts[1] || '',
                                        floor: parts[2] || '', row: parts[3] || '',
                                        seatNo: parts[4] || '', block: parts[5] || ''
                                    };
                                }

                                // Check if seat matches keyword preference
                                let keywordScore = 0;
                                if (keywords.length > 0) {
                                    const seatText = JSON.stringify(seatInfo).toLowerCase();
                                    for (const kw of keywords) {
                                        if (seatText.includes(kw.toLowerCase())) {
                                            keywordScore = 1;
                                            break;
                                        }
                                    }
                                } else {
                                    keywordScore = 1; // No keyword filter = all match
                                }

                                availableSeats.push({el, seatInfo, keywordScore, id, onclick: onclick.substring(0, 60)});
                            }

                            // Sort: keyword matches first, then by row (lower = better), then by seat number (center)
                            availableSeats.sort((a, b) => {
                                if (b.keywordScore !== a.keywordScore) return b.keywordScore - a.keywordScore;
                                const rowA = parseInt(a.seatInfo.row) || 999;
                                const rowB = parseInt(b.seatInfo.row) || 999;
                                return rowA - rowB;
                            });

                            // Click up to ticketNum seats
                            for (const seat of availableSeats) {
                                if (clickedCount >= ticketNum) break;
                                try {
                                    seat.el.click();
                                    clickedCount++;
                                    clickedSeats.push(
                                        (seat.seatInfo.floor || '') + ' R' + (seat.seatInfo.row || '?') +
                                        ' S' + (seat.seatInfo.seatNo || '?') + ' [' + (seat.seatInfo.grade || '') + ']'
                                    );
                                } catch(e) {}
                            }

                            // Recurse into iframes
                            const iframes = doc.querySelectorAll('iframe');
                            for (const ifr of iframes) {
                                if (clickedCount >= ticketNum) break;
                                try {
                                    findAndClickSeats(ifr.contentDocument || ifr.contentWindow.document, depth + 1);
                                } catch(e) {}
                            }
                        }

                        findAndClickSeats(document, 0);
                        if (clickedCount > 0) {
                            return 'seats_clicked:' + clickedCount + ' => ' + clickedSeats.join(', ');
                        }
                        return 'no_seats_clicked';
                    })()
                ''')
                print(f"[NOL-GPO] Seat click: {seat_click_result}")

                if 'seats_clicked' in str(seat_click_result):
                    # Wait a moment, then the loop will re-enter and detect "selected:" status
                    # which triggers the enhanced completion button click
                    print("[NOL-GPO] ✅ Seats clicked, will detect and click completion on next cycle")
                    await asyncio.sleep(0.3)
                    return True

            # Phase 3: Click area/block on seat map
            # Strategy: Search inside ifrmSeatDetail FIRST (not main doc),
            # use <area> tags, filtered [onclick], and grade selectors
            if has_area_map or not has_individual_seats:
                print(f"[NOL-GPO] Phase 3: Clicking area/block on seat map...")
                click_result = await tab.evaluate('''
                    (function() {
                        const keywords = ''' + area_keywords_json + ''';

                        // Blacklist: onclick handlers NOT related to seats/areas
                        const blacklist = [
                            'cancelnoticeview', 'fnclose', 'close(', 'window.close',
                            'alert(', 'confirm(', 'fncapcharefresh', 'captcha',
                            'fncancelnotice', 'history.back', 'goback', 'popup',
                            'layer', 'notice', 'help', 'guide', 'tooltip'
                        ];

                        function isBlacklisted(str) {
                            const lower = (str || '').toLowerCase();
                            return blacklist.some(b => lower.includes(b));
                        }

                        // Collect all clickable area/block candidates from a document
                        function collectCandidates(doc, frameName, depth) {
                            if (!doc || !doc.body || depth > 6) return [];
                            const candidates = [];

                            // 1. <area> elements (image map hotspots) — highest priority
                            const areaEls = doc.querySelectorAll('area');
                            for (const el of areaEls) {
                                const alt = (el.alt || el.title || '').trim();
                                const href = el.getAttribute('href') || '';
                                const onclick = el.getAttribute('onclick') || '';
                                if (isBlacklisted(onclick) || isBlacklisted(href)) continue;
                                candidates.push({
                                    el, text: alt, onclick: (onclick || href).substring(0, 80),
                                    type: 'area', priority: 10, frame: frameName
                                });
                            }

                            // 2. Elements with seat/block/grade related onclick
                            const onclickEls = doc.querySelectorAll('[onclick]');
                            for (const el of onclickEls) {
                                const onclick = el.getAttribute('onclick') || '';
                                if (isBlacklisted(onclick)) continue;

                                const lower = onclick.toLowerCase();
                                let priority = 0;
                                if (lower.includes('selectblock') || lower.includes('fnblock') || lower.includes('clickblock')) priority = 9;
                                else if (lower.includes('selectgrade') || lower.includes('fngrade') || lower.includes('gradeclick')) priority = 8;
                                else if (lower.includes('selectarea') || lower.includes('fnarea') || lower.includes('fnzone')) priority = 8;
                                else if (lower.includes('viewseat') || lower.includes('openseat') || lower.includes('seatview')) priority = 7;
                                else if (lower.includes('fnselect') || lower.includes('clickarea')) priority = 7;
                                else continue; // Skip unrecognized onclick handlers

                                const text = (el.textContent || el.alt || el.title || '').trim().substring(0, 60);
                                const id = el.id || '';
                                candidates.push({
                                    el, text, id, onclick: onclick.substring(0, 80),
                                    type: 'onclick', priority, frame: frameName
                                });
                            }

                            // 3. SVG clickable elements (some venues use SVG maps)
                            const svgEls = doc.querySelectorAll('svg [onclick], svg a, svg [cursor="pointer"]');
                            for (const el of svgEls) {
                                const onclick = el.getAttribute('onclick') || '';
                                if (isBlacklisted(onclick)) continue;
                                const text = (el.textContent || el.getAttribute('title') || '').trim().substring(0, 60);
                                if (onclick || el.tagName.toLowerCase() === 'a') {
                                    candidates.push({
                                        el, text, onclick: onclick.substring(0, 80),
                                        type: 'svg', priority: 7, frame: frameName
                                    });
                                }
                            }

                            // 4. Links with seat/block related hrefs
                            const links = doc.querySelectorAll('a[href*="seat" i], a[href*="block" i], a[href*="grade" i], a[href*="zone" i]');
                            for (const a of links) {
                                const text = (a.textContent || a.alt || '').trim().substring(0, 60);
                                const href = (a.getAttribute('href') || '').substring(0, 80);
                                if (isBlacklisted(href)) continue;
                                candidates.push({
                                    el: a, text, onclick: href,
                                    type: 'link', priority: 6, frame: frameName
                                });
                            }

                            // Recurse into iframes (search inside seat iframes first)
                            const iframes = doc.querySelectorAll('iframe');
                            const priorityIframes = [];
                            const otherIframes = [];
                            for (const ifr of iframes) {
                                const ifrId = (ifr.id || ifr.name || '').toLowerCase();
                                if (ifrId.includes('seat') || ifrId.includes('detail')) {
                                    priorityIframes.push(ifr);
                                } else {
                                    otherIframes.push(ifr);
                                }
                            }
                            for (const ifr of [...priorityIframes, ...otherIframes]) {
                                try {
                                    const ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                                    const ifrName = frameName + '>' + (ifr.id || ifr.name || 'ifr');
                                    const ifrCandidates = collectCandidates(ifrDoc, ifrName, depth + 1);
                                    candidates.push(...ifrCandidates);
                                } catch(e) {}
                            }

                            return candidates;
                        }

                        const allCandidates = collectCandidates(document, 'main', 0);

                        // Sort by priority (highest first), then prefer candidates from seat iframes
                        allCandidates.sort((a, b) => {
                            // Prefer candidates from ifrmSeatDetail
                            const aInSeat = a.frame.toLowerCase().includes('seat') ? 1 : 0;
                            const bInSeat = b.frame.toLowerCase().includes('seat') ? 1 : 0;
                            if (bInSeat !== aInSeat) return bInSeat - aInSeat;
                            return b.priority - a.priority;
                        });

                        // Debug: show all candidates
                        const debugList = allCandidates.slice(0, 15).map(c =>
                            c.type + ':' + (c.text || c.id || '').substring(0, 25) + '|' + c.onclick.substring(0, 35) + ' [' + c.frame + ']'
                        );

                        if (allCandidates.length === 0) {
                            return 'no_area_found: no_candidates debug=[]';
                        }

                        // Keyword matching
                        if (keywords.length > 0) {
                            for (const kw of keywords) {
                                const kwLower = kw.toLowerCase();
                                for (const c of allCandidates) {
                                    const matchText = ((c.text || '') + ' ' + (c.onclick || '') + ' ' + (c.id || '')).toLowerCase();
                                    if (matchText.includes(kwLower)) {
                                        c.el.click();
                                        return 'keyword_match: ' + kw + ' => ' + (c.text || c.id || '').substring(0, 40) +
                                               ' onclick=' + c.onclick.substring(0, 40) + ' [' + c.frame + '] all=' + debugList.join('; ');
                                    }
                                }
                            }
                            // Keyword specified but no match — still click first available
                            // (better to enter some area than none)
                        }

                        // No keyword or no match — click first (highest priority) candidate
                        const best = allCandidates[0];
                        best.el.click();
                        return 'auto_select: ' + (best.text || best.id || '').substring(0, 40) +
                               ' onclick=' + best.onclick.substring(0, 40) + ' type=' + best.type +
                               ' [' + best.frame + '] all=' + debugList.join('; ');
                    })()
                ''')
                print(f"[NOL-GPO] Area click: {click_result}")

                if 'no_area_found' in str(click_result):
                    # Fallback: try to find ANY clickable element in ifrmSeatDetail
                    # that has a colored background (indicating an active area)
                    fallback_result = await tab.evaluate('''
                        (function() {
                            function findColoredClickable(doc, frameName, depth) {
                                if (!doc || !doc.body || depth > 6) return null;

                                // Find elements with colored backgrounds (non-white, non-grey)
                                const els = doc.querySelectorAll('td, div, img, a, span');
                                for (const el of els) {
                                    const style = doc.defaultView ? doc.defaultView.getComputedStyle(el) : null;
                                    if (!style) continue;
                                    const bg = style.backgroundColor;
                                    const cursor = style.cursor;
                                    const onclick = el.getAttribute('onclick') || '';
                                    const hasClick = onclick || cursor === 'pointer' || el.tagName === 'A';

                                    if (!hasClick) continue;

                                    const rgbMatch = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
                                    if (rgbMatch) {
                                        const r = parseInt(rgbMatch[1]), g = parseInt(rgbMatch[2]), b = parseInt(rgbMatch[3]);
                                        // Skip white, near-white, black, near-black, grey
                                        const isNeutral = (Math.abs(r-g) < 20 && Math.abs(g-b) < 20);
                                        const isBright = r > 220 && g > 220 && b > 220;
                                        const isDark = r < 30 && g < 30 && b < 30;
                                        if (!isNeutral && !isBright && !isDark) {
                                            el.click();
                                            return 'colored_click: bg=' + bg + ' ' + (el.textContent || '').trim().substring(0, 20) +
                                                   ' [' + frameName + ']';
                                        }
                                    }
                                }

                                const iframes = doc.querySelectorAll('iframe');
                                for (const ifr of iframes) {
                                    try {
                                        const r = findColoredClickable(
                                            ifr.contentDocument || ifr.contentWindow.document,
                                            frameName + '>' + (ifr.id || ifr.name || 'ifr'), depth + 1
                                        );
                                        if (r) return r;
                                    } catch(e) {}
                                }
                                return null;
                            }
                            return findColoredClickable(document, 'main', 0) || 'no_colored_clickable';
                        })()
                    ''')
                    print(f"[NOL-GPO] Fallback colored click: {fallback_result}")

            await asyncio.sleep(0.3)
            return True

        # ---- Step 3: Price/Discount selection ----
        # After seat selection, user must select ticket count per price grade and discount type
        if info.get('hasPriceForm'):
            ticket_number = config_dict.get("ticket_number", 1)
            print(f"[NOL-GPO] 🎫 On Price/Discount step, ticket_number={ticket_number}")

            price_result = await tab.evaluate('''
                (function() {
                    const ticketNum = ''' + str(int(ticket_number) if ticket_number else 1) + ''';
                    const results = [];

                    // Search for PriceRow in main doc AND all iframes
                    function findPriceRows(doc) {
                        return doc.querySelectorAll('[id^="PriceRow"]');
                    }

                    let priceRows = findPriceRows(document);
                    let targetDoc = document;

                    // If not found in main doc, search iframes
                    if (priceRows.length === 0) {
                        const iframes = document.querySelectorAll('iframe');
                        for (const ifr of iframes) {
                            try {
                                const ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                                const ifrRows = findPriceRows(ifrDoc);
                                if (ifrRows.length > 0) {
                                    priceRows = ifrRows;
                                    targetDoc = ifrDoc;
                                    results.push('found_in_iframe:' + (ifr.id || ifr.name));
                                    break;
                                }
                            } catch(e) {}
                        }
                    }

                    if (priceRows.length === 0) {
                        // Debug: show all selects across all frames
                        const debugSelects = [];
                        document.querySelectorAll('select').forEach(s => {
                            debugSelects.push({f:'main', name: s.name || s.id, opts: Array.from(s.options).slice(0,3).map(o=>o.value)});
                        });
                        document.querySelectorAll('iframe').forEach(ifr => {
                            try {
                                const id = ifr.contentDocument || ifr.contentWindow.document;
                                id.querySelectorAll('select').forEach(s => {
                                    debugSelects.push({f:ifr.id||ifr.name, name: s.name||s.id, opts: Array.from(s.options).slice(0,3).map(o=>o.value)});
                                });
                            } catch(e) {}
                        });
                        return 'no_price_rows: selects=' + JSON.stringify(debugSelects.slice(0, 8));
                    }

                    // Each PriceRow has: grade name | price | quantity select | discount select
                    // Strategy: Set the first available price row's quantity to ticketNum
                    let filled = false;
                    for (const row of priceRows) {
                        const selects = row.querySelectorAll('select');
                        const rowText = row.textContent.trim().substring(0, 60);

                        for (const sel of selects) {
                            const name = (sel.name || sel.id || '').toLowerCase();
                            // Quantity select (not discount)
                            if (name.includes('qty') || name.includes('count') || name.includes('cnt') ||
                                name.includes('매수') || !name.includes('discount')) {
                                // Check if the desired value exists
                                const options = Array.from(sel.options);
                                const targetOpt = options.find(o => o.value === String(ticketNum));
                                if (targetOpt) {
                                    sel.value = String(ticketNum);
                                    // Trigger change event
                                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                                    results.push('qty_set:' + ticketNum + ' in ' + rowText);
                                    filled = true;
                                    break;
                                } else {
                                    // Try selecting by index (index 0 = 0 tickets, index 1 = 1 ticket, etc.)
                                    if (ticketNum < options.length) {
                                        sel.selectedIndex = ticketNum;
                                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                                        results.push('qty_idx:' + ticketNum + ' in ' + rowText);
                                        filled = true;
                                        break;
                                    }
                                }
                            }
                        }
                        if (filled) break;
                    }

                    if (!filled) {
                        // Fallback: try first select in targetDoc with numeric options
                        const allSelects = targetDoc.querySelectorAll('select');
                        for (const sel of allSelects) {
                            const options = Array.from(sel.options);
                            // Check if options look like quantities (0, 1, 2, 3...)
                            const hasNumeric = options.some(o => /^\\d+$/.test(o.value.trim()));
                            if (hasNumeric && options.length <= 20) {
                                const targetOpt = options.find(o => o.value.trim() === String(ticketNum));
                                if (targetOpt) {
                                    sel.value = String(ticketNum);
                                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                                    results.push('fallback_qty:' + ticketNum + ' name=' + (sel.name || sel.id));
                                    filled = true;
                                    break;
                                }
                            }
                        }
                    }

                    if (!filled) {
                        // Debug: show all selects in targetDoc
                        const debugSelects = [];
                        targetDoc.querySelectorAll('select').forEach(s => {
                            debugSelects.push({
                                name: s.name || s.id,
                                options: Array.from(s.options).slice(0, 5).map(o => o.value + '=' + o.text.substring(0, 15))
                            });
                        });
                        return 'no_qty_set: selects=' + JSON.stringify(debugSelects.slice(0, 5));
                    }

                    return results.join('; ');
                })()
            ''')
            print(f"[NOL-GPO] Price selection: {price_result}")

            if 'no_price_rows' not in str(price_result) and 'no_qty_set' not in str(price_result):
                await asyncio.sleep(0.2)
                # Click Next (SmallNextBtnImage) — search main doc AND iframes
                next_result = await tab.evaluate('''
                    (function() {
                        function findNextBtn(doc) {
                            const btnIds = ['SmallNextBtnImage', 'NextStepImage', 'btnNext'];
                            for (const id of btnIds) {
                                const btn = doc.getElementById(id);
                                if (btn) { btn.click(); return 'clicked: ' + id; }
                            }
                            const imgs = doc.querySelectorAll('img[src*="next" i], img[src*="Next" i]');
                            for (const img of imgs) {
                                img.click();
                                return 'clicked_img: ' + (img.src || '').substring(img.src.length - 30);
                            }
                            const btns = doc.querySelectorAll('a, button, input[type="button"], input[type="submit"]');
                            for (const btn of btns) {
                                const text = (btn.textContent || btn.value || btn.alt || '').trim().toLowerCase();
                                if (text.includes('next') || text.includes('다음') || text.includes('下一步') ||
                                    text.includes('확인') || text.includes('確認')) {
                                    btn.click();
                                    return 'clicked_text: ' + text.substring(0, 20);
                                }
                            }
                            return null;
                        }
                        var r = findNextBtn(document);
                        if (r) return r;
                        // Search iframes
                        var iframes = document.querySelectorAll('iframe');
                        for (var ifr of iframes) {
                            try {
                                var ifrDoc = ifr.contentDocument || ifr.contentWindow.document;
                                r = findNextBtn(ifrDoc);
                                if (r) return r + ' [' + (ifr.id || ifr.name) + ']';
                            } catch(e) {}
                        }
                        return 'no_next_btn';
                    })()
                ''')
                print(f"[NOL-GPO] Price next: {next_result}")
                await asyncio.sleep(0.2)
                play_sound_while_ordering(config_dict)

            return True

        # ---- Step 4: Delivery / Personal Info ----
        # After price selection, fill delivery method and personal info
        if (info.get('hasDeliveryForm') or info.get('hasPersonalInfoForm')) and not info.get('hasPriceForm'):
            print("[NOL-GPO] On Delivery/Personal Info step")

            delivery_result = await tab.evaluate('''
                (function() {
                    const results = [];

                    // 1. Select delivery method (usually first radio or select)
                    const deliveryRadios = document.querySelectorAll('input[type="radio"][name*="delivery" i], input[type="radio"][name*="Delivery" i]');
                    if (deliveryRadios.length > 0) {
                        // Select first available delivery option
                        deliveryRadios[0].click();
                        deliveryRadios[0].checked = true;
                        deliveryRadios[0].dispatchEvent(new Event('change', {bubbles: true}));
                        const label = deliveryRadios[0].closest('label') || deliveryRadios[0].closest('tr');
                        results.push('delivery: ' + (label ? label.textContent.trim().substring(0, 30) : 'option1'));
                    }

                    const deliverySelect = document.querySelector('select[name*="delivery" i], select[name*="Delivery" i]');
                    if (deliverySelect && deliverySelect.options.length > 1) {
                        deliverySelect.selectedIndex = 1; // Select first non-empty option
                        deliverySelect.dispatchEvent(new Event('change', {bubbles: true}));
                        results.push('delivery_select: ' + deliverySelect.options[1].text.substring(0, 30));
                    }

                    // 2. Check for agreement checkboxes
                    const checkboxes = document.querySelectorAll('input[type="checkbox"]');
                    for (const cb of checkboxes) {
                        const name = (cb.name || cb.id || '').toLowerCase();
                        const label = cb.closest('label') || cb.closest('tr') || cb.closest('div');
                        const labelText = label ? label.textContent.trim().toLowerCase() : '';
                        if (name.includes('agree') || name.includes('consent') || name.includes('동의') ||
                            labelText.includes('agree') || labelText.includes('동의') || labelText.includes('同意')) {
                            if (!cb.checked) {
                                cb.click();
                                cb.checked = true;
                                results.push('checkbox: ' + (name || labelText.substring(0, 20)));
                            }
                        }
                    }

                    // 3. Check page structure for debugging
                    const inputs = document.querySelectorAll('input[type="text"], input[type="email"], input[type="tel"]');
                    const inputInfo = [];
                    for (const inp of inputs) {
                        inputInfo.push({
                            name: inp.name || inp.id || '',
                            placeholder: (inp.placeholder || '').substring(0, 20),
                            value: inp.value ? 'filled' : 'empty'
                        });
                    }
                    results.push('inputs:' + JSON.stringify(inputInfo.slice(0, 8)));

                    return results.join('; ') || 'no_delivery_elements';
                })()
            ''')
            print(f"[NOL-GPO] Delivery/Info: {delivery_result}")

            # Note: Personal info (name, phone, email) should typically be pre-filled
            # or filled by the user. We don't auto-fill sensitive personal data.
            # Just click Next to proceed.
            await asyncio.sleep(1.0)

            next_result = await tab.evaluate('''
                (function() {
                    const btnIds = ['SmallNextBtnImage', 'NextStepImage', 'btnNext'];
                    for (const id of btnIds) {
                        const btn = document.getElementById(id);
                        if (btn) { btn.click(); return 'clicked: ' + id; }
                    }
                    const imgs = document.querySelectorAll('img[src*="next" i], img[src*="Next" i]');
                    for (const img of imgs) {
                        img.click();
                        return 'clicked_img: ' + (img.src || '').substring(img.src.length - 30);
                    }
                    const btns = document.querySelectorAll('a, button, input[type="button"], input[type="submit"]');
                    for (const btn of btns) {
                        const text = (btn.textContent || btn.value || btn.alt || '').trim().toLowerCase();
                        if (text.includes('next') || text.includes('다음') || text.includes('下一步') ||
                            text.includes('확인') || text.includes('確認') || text.includes('결제') ||
                            text.includes('付款') || text.includes('payment')) {
                            btn.click();
                            return 'clicked_text: ' + text.substring(0, 20);
                        }
                    }
                    return 'no_next_btn';
                })()
            ''')
            print(f"[NOL-GPO] Delivery next: {next_result}")
            await asyncio.sleep(2.0)
            play_sound_while_ordering(config_dict)
            return True

        # ---- Fallback: ifrmBookStep exists but no calendar/seatmap detected ----
        # This likely means the iframe is still loading. Also try date selection as default.
        if info.get('hasIframeBookStep') and not info.get('hasCalendar') and not info.get('hasSeatMap'):
            print(f"[NOL-GPO] ifrmBookStep exists but no calendar/seatmap detected. iframe: {info.get('iframeDebug', 'N/A')}")
            print("[NOL-GPO] Attempting date click anyway (iframe may be loading)...")

            # Try date clicking in iframe anyway (same color-based approach)
            date_result = await tab.evaluate('''
                (function() {
                    const iframe = document.getElementById('ifrmBookStep');
                    if (!iframe) return 'no_ifrmBookStep';

                    let doc;
                    try {
                        doc = iframe.contentDocument || iframe.contentWindow.document;
                    } catch(e) {
                        return 'iframe_crossorigin: ' + e.message;
                    }
                    if (!doc) return 'no_iframe_doc';

                    const bodyText = doc.body ? doc.body.textContent.substring(0, 200) : 'no_body';
                    const bodyLen = doc.body ? doc.body.innerHTML.length : 0;
                    const allTds = doc.querySelectorAll('td');

                    const candidates = [];
                    for (const td of allTds) {
                        const text = td.textContent.trim();
                        if (!/^\\d{1,2}$/.test(text)) continue;
                        const num = parseInt(text);
                        if (num < 1 || num > 31) continue;

                        const bg = doc.defaultView.getComputedStyle(td).backgroundColor;
                        const rgbMatch = bg.match(/rgb\\((\\d+),\\s*(\\d+),\\s*(\\d+)\\)/);
                        if (!rgbMatch) continue;
                        const r = parseInt(rgbMatch[1]), g = parseInt(rgbMatch[2]), b = parseInt(rgbMatch[3]);

                        const isOrange = r > 200 && g > 100 && g < 200 && b < 80;
                        const isColored = !(r > 240 && g > 240 && b > 240) && !(Math.abs(r-g)<15 && Math.abs(g-b)<15 && r>150);

                        if (isOrange || isColored) {
                            const link = td.querySelector('a');
                            candidates.push({td, link, text, bg, isOrange});
                        }
                    }

                    if (candidates.length > 0) {
                        // Prefer orange dates
                        const orange = candidates.find(c => c.isOrange);
                        const c = orange || candidates[0];
                        if (c.link) {
                            c.link.click();
                            return 'fallback_clicked: date=' + c.text + ' bg=' + c.bg + ' via=link total=' + candidates.length;
                        }
                        c.td.click();
                        return 'fallback_clicked: date=' + c.text + ' bg=' + c.bg + ' via=td total=' + candidates.length;
                    }

                    return 'fallback_no_dates: bodyLen=' + bodyLen + ' tds=' + allTds.length + ' text=' + bodyText.substring(0, 80);
                })()
            ''')
            print(f"[NOL-GPO] Fallback date: {date_result}")

            if 'clicked' in str(date_result):
                await asyncio.sleep(1.5)
                return True

        # ---- If no specific step detected, try clicking Next ----
        if info.get('hasNextBtn'):
            btn_id = info.get('nextBtnId', '')
            print(f"[NOL-GPO] Clicking next button: {btn_id}")
            await tab.evaluate(f'''
                (function() {{
                    const btn = document.getElementById('{btn_id}');
                    if (btn) btn.click();
                }})()
            ''')
            await asyncio.sleep(1.5)
            return True

        return True

    except Exception as e:
        print(f"[NOL-GPO] Booking error: {e}")
        return False


async def nodriver_nol_main(tab, url, config_dict):
    """Main entry point for NOL World platform automation."""
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    is_quit_bot = False

    try:
        # Step 1: Handle login page
        if _is_nol_login_page(url):
            # Only attempt auto-login if credentials are configured
            nol_acct = config_dict["accounts"].get("nol_account", "").strip()
            nol_pwd = config_dict["accounts"].get("nol_password", "").strip()
            if len(nol_acct) >= 4 and len(nol_pwd) >= 1:
                await nodriver_nol_signin(tab, url, config_dict)
            else:
                # No credentials — do nothing, let user login manually or set credentials
                global _login_no_cred_warned
                if not _login_no_cred_warned:
                    debug.log("[NOL] ⚠️ 帳號或密碼未設定，請到設定頁面填寫後再啟動，或手動登入")
                    _login_no_cred_warned = True
            return is_quit_bot

        # Step 2: Handle Interpark onestop booking flow
        if _is_nol_onestop_schedule(url):
            await _nol_handle_onestop_schedule(tab, url, config_dict)
            return is_quit_bot

        if _is_nol_onestop_price(url):
            try:
                await _nol_handle_onestop_price(tab, url, config_dict)
            except asyncio.CancelledError:
                print("[NOL] ✅ Page navigated after price/order")
            except Exception as e:
                print(f"[NOL] Price handling error: {e}")
            return is_quit_bot

        if _is_nol_onestop_seat(url):
            try:
                await _nol_handle_onestop_seat(tab, url, config_dict)
            except asyncio.CancelledError:
                print("[NOL] ✅ Page navigated after seat confirmation")
            except Exception as e:
                print(f"[NOL] Seat handling error: {e}")
            return is_quit_bot

        if _is_nol_onestop_checkout(url):
            result = await _nol_handle_checkout(tab, url, config_dict)
            if result:
                is_quit_bot = True
            return is_quit_bot

        # Handle other onestop pages (e.g. delivery, payment)
        if _is_nol_onestop_page(url):
            debug.log(f"[NOL] On onestop page: {url}")
            # Try to auto-click Next on unknown onestop pages
            await _nol_click_next_step(tab, debug)
            return is_quit_bot

        # Step 2.5: Handle old-style globalinterpark.com booking flow
        if _is_gpo_booking_page(url):
            try:
                await _nol_handle_gpo_booking(tab, url, config_dict)
            except asyncio.CancelledError:
                print("[NOL-GPO] ✅ Page navigated")
            except Exception as e:
                print(f"[NOL-GPO] Booking error: {e}")
            return is_quit_bot

        if _is_gpo_waiting_page(url):
            print("[NOL-GPO] On waiting page, waiting...")
            return is_quit_bot

        # Handle other globalinterpark pages
        if 'globalinterpark.com' in url:
            print(f"[NOL-GPO] On page: {url}")
            return is_quit_bot

        # Step 3: Handle event detail page on world.nol.com (click "Buy Now")
        if _is_nol_event_page(url):
            await _nol_handle_event_page(tab, url, config_dict)
            return is_quit_bot

        # Step 4: Handle seat selection on world.nol.com
        if _is_nol_seat_selection_page(url):
            await _nol_handle_seat_selection(tab, url, config_dict)
            await asyncio.sleep(random.uniform(0.3, 0.5))
            await _nol_click_next_step(tab, debug)
            return is_quit_bot

        # Step 5: Handle checkout page
        if _is_nol_booking_page(url):
            result = await _nol_handle_checkout(tab, url, config_dict)
            if result:
                is_quit_bot = True
            return is_quit_bot

        # Generic NOL page (homepage or other non-event pages)
        # After login, browser may land on homepage - redirect to event page
        if 'nol.com' in url and _is_nol_homepage(url):
            homepage = config_dict.get("homepage", "")
            if homepage and 'nol.com' in homepage:
                # Don't redirect if already on the correct page
                if url.rstrip('/') != homepage.rstrip('/'):
                    debug.log(f"[NOL] On homepage/non-event page, navigating to: {homepage}")
                    try:
                        await tab.get(homepage)
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        debug.log(f"[NOL] Navigation error: {e}")
            return is_quit_bot

        # Try to handle date selection if on any other NOL page
        if 'nol.com' in url:
            await _nol_handle_date_selection(tab, url, config_dict)

    except Exception as e:
        debug.log(f"[NOL] Main error: {e}")

    return is_quit_bot
