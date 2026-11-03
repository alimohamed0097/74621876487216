import html
import logging
import os
import random
import time
import traceback
from datetime import datetime, timezone
import requests
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

# ==============================================================================
# إعدادات البوت والتكوين الأساسي (Configuration)
# ==============================================================================

# رابط نظام حجز المواعيد الرسمي لسفارة النمسا (BMEIA)
APPOINTMENT_URL = os.getenv("APPOINTMENT_URL", "https://appointment.bmeia.gv.at/")

# بيانات تليجرام (تقرأ من GitHub Secrets كأولوية، أو تستخدم القيم الافتراضية المدمجة)
TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", "8939212306:AAH9zZh4fdJr6w3bJyhM1IYlMisMVKzisLQ"
)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "8208425337")

# هل هذه الدورة هي دورة التقرير الساعي (كل ساعة)؟
SEND_HOURLY_STATUS = os.getenv("SEND_HOURLY_STATUS", "false").strip().lower() == "true"

# إعدادات الاختيار المستهدفة حسب السيناريو المطلوب حرفياً
TARGET_LANGUAGE_OPTIONS = ["en", "English", "Englisch"]
TARGET_OFFICE = "KAIRO"
TARGET_CALENDAR_CATEGORY = "Aufenthaltsbewilligung Student (nur Bachelor)"
NO_APPOINTMENT_TEXT = "For your selection there are unfortunately no appointments available"
URGENT_KEYWORD = "ميعاد_سفارة_عاجل"

# إعداد نظام تسجيل الأحداث (Logging)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("austria_embassy_bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


# ==============================================================================
# دوال مساعدة (Helper Functions)
# ==============================================================================

def human_delay(min_sec: float = 1.5, max_sec: float = 3.5) -> None:
    """إضافة وقت انتظار عشوائي بين الخطوات لمحاكاة السلوك البشري."""
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)


def send_telegram_message(message: str) -> bool:
    """إرسال إشعار فوري عبر Telegram Bot API."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        response = requests.post(api_url, json=payload, timeout=20)
        response.raise_for_status()
        logging.info("تم إرسال إشعار تليجرام بنجاح.")
        return True
    except requests.RequestException as e:
        logging.error("فشل إرسال إشعار تليجرام: %s", e)
        return False


def select_dropdown_option(page: Page, selector: str, target_text: str) -> str:
    """
    البحث عن خيار داخل القائمة المنسدلة عبر مطابقة النص أو القيمة (غير حساس لحالة الأحرف)
    واختياره باستخدام Playwright.
    """
    select_loc = page.locator(selector).first
    select_loc.wait_for(state="visible", timeout=20000)

    options = select_loc.locator("option").all()
    target_lower = target_text.strip().lower()

    for option in options:
        opt_text = (option.inner_text() or "").strip()
        opt_val = (option.get_attribute("value") or "").strip()
        if (
            target_lower == opt_text.lower()
            or target_lower in opt_text.lower()
            or target_lower == opt_val.lower()
        ):
            if opt_val:
                select_loc.select_option(value=opt_val)
            else:
                select_loc.select_option(label=opt_text)
            logging.info("تم اختيار '%s' من القائمة (%s).", opt_text, selector)
            return opt_text

    raise RuntimeError(
        f"تعذر العثور على الخيار '{target_text}' داخل القائمة المنسدلة ({selector})."
    )


def ensure_english_language(page: Page) -> None:
    """
    الخطوة 2: اختيار اللغة الإنجليزية من القائمة المنسدلة الخاصة باللغة،
    ثم الضغط على زر الإدخال (input/submit) المسؤول عن تأكيد تغيير اللغة.
    """
    logging.info("التحقق من القائمة المنسدلة الخاصة باللغة واختيار الإنجليزية...")
    lang_selectors = [
        "select#Language",
        "select[name='Language']",
        "select#Sprache",
        "select[name='Sprache']",
        "form select:has(option[value='en'])",
        "form select:has(option:has-text('English'))",
    ]

    lang_select = None
    for sel in lang_selectors:
        loc = page.locator(sel).first
        if loc.count() > 0 and loc.is_visible():
            lang_select = loc
            break

    if not lang_select:
        logging.warning("لم يتم رصد قائمة لغة منفصلة أو أن الموقع بالفعل باللغة الإنجليزية.")
        return

    # اختيار اللغة الإنجليزية
    selected = False
    options = lang_select.locator("option").all()
    for option in options:
        opt_text = (option.inner_text() or "").strip()
        opt_val = (option.get_attribute("value") or "").strip()
        if any(
            t.lower() == opt_val.lower() or t.lower() in opt_text.lower()
            for t in TARGET_LANGUAGE_OPTIONS
        ):
            if opt_val:
                lang_select.select_option(value=opt_val)
            else:
                lang_select.select_option(label=opt_text)
            logging.info("تم اختيار اللغة الإنجليزية: %s", opt_text or opt_val)
            selected = True
            break

    if not selected:
        raise RuntimeError("تعذر العثور على اللغة الإنجليزية في القائمة المنسدلة للغة.")

    human_delay(1.0, 2.0)

    # الضغط على زر تأكيد تغيير اللغة (input/submit) المرتبط بنموذج اللغة
    parent_form = lang_select.locator("xpath=ancestor::form[1]")
    submit_btn = parent_form.locator("input[type='submit'], button[type='submit']").first

    if submit_btn.count() > 0 and submit_btn.is_visible():
        logging.info("الضغط على زر تأكيد تغيير اللغة...")
        submit_btn.click()
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    else:
        # في حال كان زر تأكيد اللغة منفصلاً خارج النموذج أو يعتمد على أول زر submit
        fallback_btn = page.locator(
            "input[type='submit'][value*='Change'], "
            "input[type='submit'][value*='Sprache'], "
            "input[type='submit'][value*='Language'], "
            "input[type='submit']:not([value='Next']):not([value='Weiter'])"
        ).first
        if fallback_btn.count() > 0 and fallback_btn.is_visible():
            logging.info("الضغط على زر تأكيد اللغة البديل...")
            fallback_btn.click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)


def click_next_button(page: Page, step_name: str) -> None:
    """البحث عن زر التالي (Next) والضغط عليه مع انتظار تحميل الصفحة التالية."""
    next_selectors = [
        "input[type='submit'][value='Next']",
        "input[type='submit'][value*='Next']",
        "input[type='submit'][value*='Weiter']",
        "input[type='submit'][name='Command']",
        "button:has-text('Next')",
        "button:has-text('Weiter')",
    ]

    for selector in next_selectors:
        loc = page.locator(selector)
        count = loc.count()
        for i in range(count):
            elem = loc.nth(i)
            if elem.is_visible() and elem.is_enabled():
                logging.info("الضغط على زر التالي (%s)...", step_name)
                elem.click()
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                return

    raise RuntimeError(f"تعذر العثور أو الضغط على زر التالي (Next) في خطوة: {step_name}")


# ==============================================================================
# المهمة الرئيسية لفحص المواعيد (Core Monitoring Workflow)
# ==============================================================================

def check_appointments() -> None:
    """تنفيذ دورة فحص كاملة لموقع مواعيد سفارة النمسا باستخدام Playwright."""
    logging.info("بدء دورة فحص جديدة لمواعيد سفارة النمسا (Playwright Headless)...")

    with sync_playwright() as p:
        browser = None
        try:
            # تشغيل المتصفح بوضع Headless مع إعدادات تخطي الحماية
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                    "--window-size=1920,1080",
                    "--ignore-certificate-errors",
                ],
            )

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
                locale="en-US",
            )

            # إخفاء مؤشر الأتمتة webdriver
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

            page = context.new_page()
            page.set_default_timeout(30000)

            # 1. الدخول إلى الرابط الأساسي
            logging.info("1. الانتقال إلى الرابط: %s", APPOINTMENT_URL)
            page.goto(APPOINTMENT_URL, wait_until="domcontentloaded", timeout=60000)
            human_delay(2.0, 3.5)

            # 2. اختيار اللغة الإنجليزية والضغط على زر تأكيد تغيير اللغة
            logging.info("2. تنفيذ خطوة تغيير اللغة إلى الإنجليزية...")
            ensure_english_language(page)
            human_delay(1.5, 3.0)

            # 3. اختيار المكتب KAIRO والضغط على زر التالي
            logging.info("3. اختيار المكتب (%s) من قائمة Office...", TARGET_OFFICE)
            select_dropdown_option(
                page, "select#Office, select[name='Office']", TARGET_OFFICE
            )
            human_delay(1.5, 2.5)
            click_next_button(page, "الخطوة 3 - بعد اختيار المكتب KAIRO")
            human_delay(2.0, 3.5)

            # 4. اختيار نوع التأشيرة Aufenthaltsbewilligung Student (nur Bachelor) والضغط على زر التالي
            logging.info("4. اختيار نوع التأشيرة (%s)...", TARGET_CALENDAR_CATEGORY)
            select_dropdown_option(
                page,
                "select#CalendarId, select[name='CalendarId']",
                TARGET_CALENDAR_CATEGORY,
            )
            human_delay(1.5, 2.5)
            click_next_button(page, "الخطوة 4 - بعد اختيار نوع التأشيرة")
            human_delay(2.0, 3.5)

            # 5. الصفحة التالية: الضغط على زر التالي مباشرة
            logging.info("5. الضغط على زر التالي مباشرة (صفحة الإرشادات/عدد الأشخاص)...")
            click_next_button(page, "الخطوة 5 - الانتقال المباشر الأول")
            human_delay(2.0, 3.5)

            # 6. الصفحة التي تليها: الضغط على زر التالي مباشرة للوصول إلى الصفحة الأخيرة
            logging.info("6. الضغط على زر التالي مباشرة للوصول إلى الصفحة الأخيرة...")
            click_next_button(page, "الخطوة 6 - الوصول إلى الصفحة الأخيرة")
            human_delay(2.0, 3.5)

            # 7. فحص النص الظاهر على الشاشة في الصفحة الأخيرة
            logging.info("7. جاري فحص النص الظاهر في الصفحة الأخيرة...")
            body_text = page.locator("body").inner_text().strip()
            page_content = page.content()

            # تنظيف النص الظاهر لإرساله في الرسالة عند الحاجة (أول 1200 حرف)
            cleaned_lines = [
                line.strip() for line in body_text.splitlines() if line.strip()
            ]
            screen_excerpt = "\n".join(cleaned_lines)[:1200]
            safe_excerpt = html.escape(screen_excerpt)

            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            if (
                NO_APPOINTMENT_TEXT.lower() in body_text.lower()
                or NO_APPOINTMENT_TEXT.lower() in page_content.lower()
            ):
                logging.info(
                    "النتيجة: الجملة موجودة (%s). لا توجد مواعيد متاحة حالياً.",
                    NO_APPOINTMENT_TEXT,
                )
                # إرسال رسالة التأكيد الساعية فقط إذا كانت هذه الدورة هي دورة رأس الساعة
                if SEND_HOURLY_STATUS:
                    logging.info("إرسال التقرير الساعي عبر تليجرام (الكود يعمل بخير ولا يوجد مواعيد)...")
                    hourly_msg = (
                        "✅ <b>تقرير الساعة: الكود يعمل بخير ولا يوجد مواعيد حالياً</b>\n\n"
                        f"🏢 <b>المكتب:</b> {TARGET_OFFICE}\n"
                        f"🎓 <b>الفئة:</b> {TARGET_CALENDAR_CATEGORY}\n"
                        f"⏰ <b>وقت الفحص:</b> {now_str}\n"
                        f"📌 <b>الحالة:</b> تم الوصول للصفحة الأخيرة بنجاح وظهرت جملة عدم توفر مواعيد."
                    )
                    send_telegram_message(hourly_msg)
            else:
                logging.warning("تم رصد اختفاء جملة عدم وجود مواعيد! جاري إرسال إنذار عاجل...")
                urgent_msg = (
                    f"🚨 <b>{URGENT_KEYWORD}</b> 🚨\n\n"
                    "🔥 <b>المواعيد متاحة الآن في سفارة النمسا!</b>\n\n"
                    f"🏢 <b>المكتب:</b> {TARGET_OFFICE}\n"
                    f"🎓 <b>الفئة:</b> {TARGET_CALENDAR_CATEGORY}\n"
                    f"⏰ <b>وقت الرصد:</b> {now_str}\n\n"
                    f"🔗 <b>رابط موقع الحجز:</b>\n{APPOINTMENT_URL}\n\n"
                    f"📄 <b>النص الظاهر على الشاشة حالياً:</b>\n<pre>{safe_excerpt}</pre>"
                )
                send_telegram_message(urgent_msg)

        except PlaywrightTimeoutError as e:
            logging.error("انتهت مهلة الانتظار (Timeout) أثناء تصفح الموقع: %s", e)
            err_details = html.escape(str(e))
            error_msg = (
                "⚠️ <b>تنبيه عطل في بوت سفارة النمسا (Timeout Error)</b>\n\n"
                "حدث تأخر أو تعليق أثناء تحميل إحدى صفحات الموقع أو تغير في العناصر.\n\n"
                f"🛠 <b>تفاصيل الخطأ:</b>\n<pre>{err_details}</pre>\n"
                f"🔗 <b>الرابط:</b> {APPOINTMENT_URL}"
            )
            send_telegram_message(error_msg)
            raise

        except Exception as e:
            logging.error("حدث خطأ أثناء الفحص: %s", e)
            tb_excerpt = html.escape("".join(traceback.format_exception_only(type(e), e)).strip())
            error_msg = (
                "❌ <b>تنبيه عطل في بوت سفارة النمسا (Script Error)</b>\n\n"
                "حدث خلل في الموقع أو تغير في التصميم أو تعطل الكود أثناء الفحص.\n\n"
                f"🛠 <b>تفاصيل الخطأ (Error message):</b>\n<pre>{tb_excerpt}</pre>\n"
                f"🔗 <b>الرابط:</b> {APPOINTMENT_URL}"
            )
            send_telegram_message(error_msg)
            raise

        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass


if __name__ == "__main__":
    check_appointments()