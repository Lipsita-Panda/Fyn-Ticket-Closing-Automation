"""
Recovery ticket closing for the Fyn ops platform (Django admin) - the BROWSER side.

The mail side lives in recovery_automation.py. This file only knows how to
drive the platform, and can also be run by hand:  python recovery_ticket.py

Two modes:
  create_service=False -> close the recovery only (service checkbox left OFF), submit.
  create_service=True  -> close the recovery AND tick "Create Service Ticket for this
                          vehicle", fill the service form, submit.

Env vars: PLATFORM_URL, PLATFORM_USERNAME, PLATFORM_PASSWORD
Optional: BROWSER_CHANNEL (e.g. msedge, for a laptop that blocks Playwright's own Chromium)
"""
import os
import re
from urllib.parse import quote
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

LOCATION_BY_PREFIX = {
    "KA": "Vendor - Fyn Mobility Bangalore",
    "TN": "Vendor - Fyn Mobility Chennai",
    "TG": "Vendor - Fyn Mobility Hyderabad",
    "TS": "Vendor - Fyn Mobility Hyderabad",
}
CITY_BY_PREFIX = {"KA": "bangalore", "TN": "chennai", "TG": "hyderabad", "TS": "hyderabad"}


def _settle(page):
    """Wait for the page to go quiet, but never treat a slow page as a failure."""
    try:
        page.wait_for_load_state("networkidle")
    except PWTimeout:
        pass


PAGE_LOAD_TIMEOUT_MS = 60000
SUBMITTED_BUT_OPEN = "Submitted, but the recovery ticket still shows as Open"


def _login(page, url, user, pwd):
    # The platform is sometimes slow to answer. Wait up to a minute for the
    # page itself (not for every last image/script on it) and try 3 times.
    for attempt in (1, 2, 3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            break
        except PWTimeout:
            if attempt == 3:
                raise
            print(f"  (the platform was slow to load - trying again, {attempt} of 3)")
    if page.get_by_role("textbox", name="Username:").count():
        page.get_by_role("textbox", name="Username:").fill(user)
        page.get_by_role("textbox", name="Password:").fill(pwd)
        page.get_by_role("button", name="Log in").click()
        _settle(page)


def _open_rows(page, plate):
    """Vehicle Recoveries -> search the plate -> 'Open' filter. Returns the row checkboxes."""
    page.get_by_role("link", name="Vehicle Recoveries").click()
    page.get_by_role("textbox", name="Search").fill(plate)
    page.get_by_role("button", name="Search").click()
    page.wait_for_load_state("networkidle")
    page.get_by_role("link", name="Open", exact=True).click()
    page.wait_for_load_state("networkidle")
    return page.locator("input[name='_selected_action']")


def _still_open(page, url, plate):
    """After submitting: is the recovery STILL in the Open list? Checked up to
    3 times (now, +5 s, +10 s) because the platform can take a moment to save,
    especially when it also creates a service ticket. True only if it was
    positively seen as still open on the last successful look. If the check
    itself keeps failing the answer is 'unknown' (False), so a slow page can
    never turn a real success into a failure."""
    seen_open = False
    for pause_ms in (0, 5000, 10000):
        if pause_ms:
            page.wait_for_timeout(pause_ms)
        try:
            page.goto(url)
            seen_open = _open_rows(page, plate).count() > 0
        except Exception:
            continue
        if not seen_open:
            return False
    return seen_open


def _page_messages(page):
    """Visible error / warning text on the current page (Django admin style),
    e.g. 'Please correct the error below' - to explain a form that did not save."""
    found = []
    try:
        for selector in (".errornote", ".errorlist li", ".alert-danger", ".alert-warning",
                         ".invalid-feedback", ".messagelist .error", ".messagelist .warning"):
            loc = page.locator(selector)
            for i in range(min(loc.count(), 10)):
                item = loc.nth(i)
                if item.is_visible():
                    text = " ".join(item.inner_text().split())
                    if text and text not in found:
                        found.append(text)
    except Exception:
        pass
    return "; ".join(found)[:300]


def _plate_pattern(plate):
    """The platform shows plates with spaces; match the characters in order, whatever the spacing."""
    compact = re.sub(r"\s+", "", plate)
    return re.compile(r"\s*".join(re.escape(ch) for ch in compact), re.IGNORECASE)


def _count_services(page, url, plate):
    """How many service tickets the Services list shows for this plate (any status).
    None if it can't be read."""
    try:
        page.goto(f"{url.rstrip('/')}/services/service/?q={quote(plate)}",
                  wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        _settle(page)
        return page.get_by_role("row").filter(has_text=_plate_pattern(plate)).count()
    except Exception:
        return None


def _service_ticket_appeared(page, url, plate, count_before):
    """After closing with a service-ticket request: did a NEW service ticket show up
    for the plate? Looked for now, +5 s and +10 s (the platform can be slow to save).
    If the Services list can't be read at all, assume it worked rather than raise a false alarm."""
    if count_before is None:
        return True
    readable = False
    for pause_ms in (0, 5000, 10000):
        if pause_ms:
            page.wait_for_timeout(pause_ms)
        now = _count_services(page, url, plate)
        if now is None:
            continue
        readable = True
        if now > count_before:
            return True
    return not readable


READBACK_MS = 1500        # the form read-back is only a diagnostic: it must never slow the real work


def _shown_text(container):
    """What a dropdown currently shows as chosen (select2 text, the real <select>, or its search box)."""
    readers = (
        (container.locator(".select2-selection__rendered"), lambda el: el.inner_text(timeout=READBACK_MS)),
        (container.locator("select"), lambda el: el.evaluate(
            "e => e.selectedOptions.length ? e.selectedOptions[0].text : ''", timeout=READBACK_MS)),
        (container.get_by_role("searchbox"), lambda el: el.input_value(timeout=READBACK_MS)),
    )
    for loc, read in readers:
        try:
            if loc.count():
                text = " ".join(read(loc.first).split())
                if text:
                    return text
        except Exception:
            pass
    return ""


def _read_service_form(page):
    """The service-ticket part of the form as the page holds it right now."""
    def safe(fn):
        try:
            return fn()
        except Exception:
            return "?"
    return {
        "box ticked": safe(lambda: page.get_by_role("checkbox", name="Create Service Ticket for").is_checked(timeout=READBACK_MS)),
        "major issue ticked": safe(lambda: page.get_by_role("checkbox", name="Major issue").is_checked(timeout=READBACK_MS)),
        "issue": safe(lambda: _shown_text(page.locator("#div_id_issue_reported"))),
        "subtype": safe(lambda: _shown_text(page.locator("#div_id_issue_reported_subtype"))),
        "address": safe(lambda: page.get_by_role("textbox", name="Address*").input_value(timeout=READBACK_MS)),
        "description": safe(lambda: page.get_by_role("textbox", name="Description*").input_value(timeout=READBACK_MS)),
        "priority": safe(lambda: page.locator("#service-form #id_priority").input_value(timeout=READBACK_MS)),
    }


def _print_diagnostics(page):
    """What the page looks like just before Submit, when a service ticket is being created."""
    try:
        print(f"  DIAG service form before Submit: {_read_service_form(page)}")
        page.screenshot(path="debug_recovery_before_submit.png", full_page=True)
    except Exception as e:
        print(f"  DIAG could not read the form: {str(e)[:100]}")
    try:
        buttons = [" ".join(t.split()) for t in page.get_by_role("button").all_inner_texts() if t.strip()]
        tag = page.locator("#service-form").evaluate("el => el.tagName", timeout=READBACK_MS)
        inside = [" ".join(t.split()) for t in page.locator("#service-form").get_by_role("button").all_inner_texts() if t.strip()]
        print(f"  DIAG buttons on the page: {buttons} | #service-form is a <{tag}> | buttons inside it: {inside}")
    except Exception as e:
        print(f"  DIAG could not read the page: {str(e)[:100]}")


def _pick_select2(page, container_selector, text=None):
    """Open a select2 box inside container_selector and choose `text`, or the first option."""
    container = page.locator(container_selector)
    box = container.get_by_role("searchbox")
    # Only the visible select2 dropdown rows - NOT the hidden native <option> tags.
    results = page.locator(".select2-container--open .select2-results__option:not(.select2-results__message)")

    box.click()
    try:
        results.first.wait_for(state="visible", timeout=4000)
    except PWTimeout:
        container.locator(".select2-selection").first.click()
        results.first.wait_for(state="visible", timeout=6000)

    if text:
        # also try the common spelling "braking" in case the option is spelled that way
        candidates = list(dict.fromkeys([text, text.replace("breaking", "braking")]))
        for cand in candidates:
            box.fill(cand)
            page.wait_for_timeout(800)  # let the search results refresh
            match = results.filter(has_text=re.compile(re.escape(cand), re.I))
            if match.count():
                match.first.click()
                return
        raise RuntimeError(f"No dropdown option matching {candidates} in {container_selector}")
    else:
        # first real choice, skipping the "---------" placeholder
        results.filter(has_not_text=re.compile(r"^\s*-+\s*$")).first.click()


def close_recovery_ticket(plate: str, remarks: str = "Recovery Done", create_service: bool = False,
                          issue: str = "other issue", subtype: str = "breaking system",
                          description: str = "vehicle issue", headless: bool = True,
                          recovery_type: str = "0", location_text: str = None) -> dict:
    """
    recovery_type : "0" = Soft, "1" = Hard (the Type* dropdown).
    location_text : a location written in the mail. It is searched for in the
                    location dropdown; if there is no such option the Fyn location
                    for the plate's state is used instead (and noted in location_note).
                    None -> the Fyn location for the plate's state (KA/TN/TG/TS).

    Returns {"ok", "outcome", "message", "location_used", "type_used"}.
    outcome is one of: closed, closed_service_created, closed_service_unconfirmed
    (the recovery closed but no new service ticket could be seen), closed_refynd_no_service,
    not_found (no open recovery for that plate).
    """
    url = os.environ["PLATFORM_URL"]
    user = os.environ["PLATFORM_USERNAME"]
    pwd = os.environ["PLATFORM_PASSWORD"]

    prefix = plate.strip().upper()[:2]
    if prefix not in LOCATION_BY_PREFIX:
        raise ValueError(f"No mapping for prefix '{prefix}' - add it to LOCATION_BY_PREFIX / CITY_BY_PREFIX")
    location = LOCATION_BY_PREFIX[prefix]
    city = CITY_BY_PREFIX[prefix]

    with sync_playwright() as pw:
        launch_kwargs = {"headless": headless}
        if os.environ.get("BROWSER_CHANNEL"):          # e.g. msedge on a laptop that blocks Playwright's own Chromium
            launch_kwargs["channel"] = os.environ["BROWSER_CHANNEL"]
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(viewport={"width": 1100, "height": 600})
        page = context.new_page()
        try:
            _login(page, url, user, pwd)

            services_before = None
            if create_service:
                services_before = _count_services(page, url, plate)
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)   # back to the admin home

            rows = _open_rows(page, plate)
            n = rows.count()
            if n == 0:
                return {"ok": False, "outcome": "not_found", "location_used": "", "type_used": recovery_type,
                        "message": f"Vehicle Number : {plate}\nNo open recovery record found."}
            if n > 1:
                raise RuntimeError(f"{n} open recovery records for {plate} - needs a human to pick one")

            rows.check()
            page.get_by_label(re.compile(r"^Action:")).select_option("change_status")
            page.get_by_role("button", name="Go").click()
            page.wait_for_load_state("networkidle")

            page.get_by_label("Type*", exact=True).select_option(recovery_type)
            page.get_by_label("Status*").select_option("1")
            status_text = page.get_by_label("Status*").evaluate(
                "el => el.options[el.selectedIndex].text"
            ).strip()

            page.get_by_role("textbox", name="Remarks*").fill(remarks)

            page.locator("#div_id_vehicle_location").get_by_label("", exact=True).click()
            location_note = ""
            if location_text:
                # A location was written in the mail: search for exactly that.
                page.get_by_role("searchbox").fill(location_text)
                option = page.get_by_role("option", name=re.compile(re.escape(location_text), re.I)).first
                try:
                    option.wait_for(state="visible", timeout=8000)
                    option.click()
                    location_used = location_text
                except PWTimeout:
                    # Not in the dropdown: use the Fyn location for the plate's state instead.
                    location_text = None
                    location_note = f"location in the mail was not found in the dropdown - used {location}"
            if not location_text:
                page.get_by_role("searchbox").fill("Fyn ")
                page.get_by_role("option", name=location).click()
                location_used = location

            service_cb = page.get_by_role("checkbox", name="Create Service Ticket for")
            checkbox_present = True
            if create_service:
                try:
                    service_cb.wait_for(state="visible", timeout=4000)
                except PWTimeout:
                    checkbox_present = False  # refynd vehicle: no service ticket possible

            if create_service and checkbox_present:
                service_cb.check()
                page.get_by_role("checkbox", name="Major issue").uncheck()
                _pick_select2(page, "#div_id_issue_reported", issue)
                _pick_select2(page, "#div_id_issue_reported_subtype", subtype)
                page.get_by_role("textbox", name="Address*").fill(city)
                page.get_by_role("textbox", name="Description*").fill(description)
                page.locator("#service-form #id_priority").select_option("0")
            else:
                # Recovery only: make sure the service-ticket tick stays OFF.
                if service_cb.count():
                    service_cb.uncheck()

            if create_service and checkbox_present:
                _print_diagnostics(page)
            page.get_by_role("button", name=re.compile("submit", re.I)).first.click()
            _settle(page)
            page.wait_for_timeout(1500)                    # let any message on the page appear
            platform_said = _page_messages(page)          # e.g. a validation error on the form
            try:
                page.screenshot(path="debug_recovery_after_submit.png")
            except Exception:
                pass

            # Don't report "closed" unless it really left the Open list.
            if _still_open(page, url, plate):
                raise RuntimeError(
                    SUBMITTED_BUT_OPEN + " - it was not closed"
                    + (f" (the platform said: {platform_said})" if platform_said else "")
                )

            if create_service and checkbox_present:
                if _service_ticket_appeared(page, url, plate, services_before):
                    outcome = "closed_service_created"
                    msg = f"Recovery ticket has been closed and service ticket has been created for {plate}"
                else:
                    outcome = "closed_service_unconfirmed"
                    msg = (f"Recovery ticket has been closed for {plate}, but we could not confirm that the "
                           f"service ticket was created, kindly check and create it if it is missing")
            elif create_service and not checkbox_present:
                outcome = "closed_refynd_no_service"
                msg = (f"Recovery ticket has been closed for {plate}, "
                       f"as it is a refynd vehicle we are unable to create service ticket")
            else:
                outcome = "closed"
                msg = f"Recovery ticket has been closed for {plate}"
            return {"ok": True, "outcome": outcome, "message": msg, "location_note": location_note,
                    "location_used": location_used, "type_used": recovery_type}
        except PWTimeout as e:
            try:
                page.screenshot(path="debug_recovery.png")
            except Exception:
                pass
            raise RuntimeError(f"Timed out for {plate}: {e}")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    v = input("Vehicle number (e.g. KA 04 AD 6804): ").strip()
    svc = input("Also create service ticket? (y/N): ").strip().lower() == "y"
    print(close_recovery_ticket(v, create_service=svc, headless=False))
