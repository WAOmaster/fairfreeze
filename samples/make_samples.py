"""Renders the synthetic demo documents (HTML -> PNG with headless Chrome) and writes library.json.
Every person, bank, company, case number and account is fictional. Each image carries a SYNTHETIC watermark.
Run: python samples/make_samples.py
"""
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

OUT = Path(__file__).parent
ROOT = OUT.parent
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

BASE = """<html><head><meta charset="utf-8"><style>
body{margin:0;font-family:'Segoe UI','Nirmala UI','Leelawadee UI','Microsoft YaHei',sans-serif;background:#fff;color:#111}
.wm{position:fixed;top:45%;left:-10%;width:120%;text-align:center;transform:rotate(-24deg);font-size:30px;
 color:rgba(200,0,0,.13);font-weight:700;letter-spacing:2px;pointer-events:none}
.doc{padding:46px 56px;font-size:16px;line-height:1.55}
.doc h1{font-size:20px;text-align:center;margin:0 0 4px}.doc .sub{text-align:center;color:#444;margin-bottom:22px}
.doc table{border-collapse:collapse;width:100%;margin:14px 0}.doc td{border:1px solid #999;padding:6px 9px;vertical-align:top}
.chat{width:420px;min-height:820px;background:#ece5dd}.bar{background:VAR;color:#fff;padding:14px 16px;font-weight:600}
.bar small{display:block;font-weight:400;opacity:.85}.listing{background:#fff;margin:10px;padding:10px;border-radius:8px;font-size:14px}
.m{max-width:78%;margin:8px 10px;padding:8px 11px;border-radius:9px;font-size:15px;line-height:1.4;clear:both}
.l{background:#fff;float:left}.r{background:#dcf8c6;float:right}.t{display:block;font-size:11px;color:#777;text-align:right}
.day{clear:both;text-align:center;font-size:12px;color:#555;padding-top:8px}
</style></head><body><div class="wm">SYNTHETIC DEMO DOCUMENT · FairFreeze · NOT A REAL RECORD</div>BODY</body></html>"""


def chat(title, sub, color, listing, msgs, day):
    lst = f'<div class="listing">{listing}</div>' if listing else ""
    body = "".join(f'<div class="m {side}">{text}<span class="t">{time}</span></div>' for side, text, time in msgs)
    return BASE.replace("VAR", color).replace(
        "BODY", f'<div class="chat"><div class="bar">{title}<small>{sub}</small></div>{lst}<div class="day">{day}</div>{body}</div>')


def doc(html):
    return BASE.replace("BODY", f'<div class="doc">{html}</div>')


IN_NOTICE = """<h1>CYBER POLICE STATION, PUNE CITY &nbsp;|&nbsp; साइबर पुलिस स्टेशन, पुणे शहर</h1>
<div class="sub">Notice under Section 106 BNSS read with NCRP / CFCFRMS procedure</div>
<p>No. CPS/PUNE/{no}/2026 &nbsp;&nbsp;&nbsp; Date: {date}</p>
<p>To,<br>The Branch Manager,<br>Sahyadri Cooperative Bank, Kothrud Branch, Pune</p>
<p><b>Subject:</b> Debit freeze of account in connection with NCRP complaint No. {ack}</p>
<p>A complaint of online fraud has been registered on the National Cybercrime Reporting Portal. Analysis of the money trail shows that the
amount below was credited to the account maintained with your bank. You are requested to <b>impose a debit freeze on the account</b>
mentioned below with immediate effect and to furnish KYC documents and the account statement to this office.</p>
<table><tr><td>Account number</td><td>{acct}</td></tr><tr><td>Account holder</td><td>{holder}</td></tr>
<tr><td>Transaction</td><td>UPI reference {utr} dated {txdate}</td></tr><tr><td>Amount</td><td>Rs. {amt}/-</td></tr>
<tr><td>Layer</td><td>{layer}</td></tr></table>
<p>Yours faithfully,<br><br><b>{officer}</b><br>Police Inspector, Cyber Police Station, Pune City<br>Phone 020-2XXXXXXX</p>"""

TH_NOTICE = """<h1>หนังสือแจ้งระงับการทำธุรกรรมชั่วคราว</h1>
<div class="sub">กองบังคับการปราบปรามการกระทำความผิดเกี่ยวกับอาชญากรรมทางเทคโนโลยี (สมมติเพื่อการสาธิต)</div>
<p>ที่ ตช 0026.7/69-4412 &nbsp;&nbsp;&nbsp; วันที่ 17 ตุลาคม 2569</p>
<p>เรียน ผู้จัดการ ธนาคารล้านนาพาณิชย์ (สมมติ)</p>
<p>อาศัยอำนาจตามพระราชกำหนดมาตรการป้องกันและปราบปรามอาชญากรรมทางเทคโนโลยี พ.ศ. 2566
ขอให้ธนาคาร<b>ระงับการทำธุรกรรมทั้งบัญชี</b>ของบัญชีด้านล่างเป็นการชั่วคราว เนื่องจากได้รับเงินโอนที่เกี่ยวข้องกับคดีหลอกลวงทางออนไลน์
เลขรับแจ้งความออนไลน์ 6910150081234</p>
<table><tr><td>เลขที่บัญชี</td><td>LCB-221-5-80734</td></tr><tr><td>ชื่อบัญชี</td><td>นางสาวนก ศรีสุวรรณ</td></tr>
<tr><td>รายการ</td><td>รับโอนพร้อมเพย์ จาก NATTAPONG S. วันที่ 15 ตุลาคม 2569 เวลา 14:20 น.</td></tr>
<tr><td>จำนวนเงิน</td><td>1,250 บาท</td></tr></table>
<p>ขอแสดงความนับถือ<br><br><b>พ.ต.ท. ธนกร วงศ์ใหญ่</b><br>สารวัตร กองกำกับการ 2</p>"""

SG_ORDER = """<h1>RESTRICTION ORDER</h1><div class="sub">Protection from Scams Act 2025 &nbsp;·&nbsp; Anti-Scam Command (synthetic demo)</div>
<p>Order No. ASCOM/RO/2026/00771 &nbsp;&nbsp;&nbsp; Date of issue: 16 October 2026</p>
<p>To: Merlion Savings Bank Ltd (fictional)</p>
<p>Having reason to believe that the individual named below is likely to transfer money to scammers, the Bank is directed to restrict the
following for <b>30 days</b> from the date of issue, unless renewed:</p>
<table><tr><td>Name</td><td>TAN MEI LING</td></tr><tr><td>Account</td><td>MSB-301-448812-9</td></tr>
<tr><td>Restricted</td><td>All outgoing transfers and remittances, new payees, overseas transfers, new credit facilities</td></tr>
<tr><td>Valid</td><td>16 Oct 2026 to 14 Nov 2026</td></tr></table>
<p>The individual may approach the Bank to access money for legitimate needs, including daily living expenses.</p>
<p><b>ASP Lim Wei Jie</b><br>Anti-Scam Command</p>"""

COURIER = """<h1>DakExpress Courier &nbsp;·&nbsp; Booking Receipt</h1><div class="sub">(fictional courier, demo only)</div>
<table><tr><td>AWB No.</td><td>DX78812004</td></tr><tr><td>Booked on</td><td>12-10-2026 10:14</td></tr>
<tr><td>Sender</td><td>Ravi Deshmukh, Kothrud, Pune 411038</td></tr><tr><td>Receiver</td><td>Amit Sharma, Dharampeth, Nagpur 440010</td></tr>
<tr><td>Contents</td><td>Mobile phone (Redmi Note 12, used) with box</td></tr><tr><td>Declared value</td><td>Rs. 3,600</td></tr>
<tr><td>Charges paid</td><td>Rs. 140</td></tr></table>"""

RAVI = [("l", "Redmi Note 12 abhi available hai?", "18:21"),
        ("r", "हाँ, available है। 6 महीने पुराना, बॉक्स और बिल के साथ।", "18:24"),
        ("l", "3600 final? Nagpur courier kar doge?", "18:30"),
        ("r", "ठीक है, 3600 final. Payment ke baad kal subah courier kar dunga.", "18:33"),
        ("l", "UPI kar diya bhai, check karo", "18:43"),
        ("r", "मिल गया, धन्यवाद। Courier receipt kal bhej dunga.", "18:47")]

NOK = [("l", "สวัสดีครับ กระเป๋าผ้าทอสีน้ำเงินยังมีไหมครับ", "13:58"),
       ("r", "มีค่ะ ราคา 1,250 บาท รวมค่าส่งค่ะ", "14:02"),
       ("l", "โอนแล้วครับ ชื่อ ณัฐพงษ์ ส.", "14:21"),
       ("r", "ได้รับแล้วค่ะ ขอบคุณมากค่ะ จะส่งพรุ่งนี้เช้านะคะ", "14:25"),
       ("r", "เลขพัสดุ TH2026101588 ค่ะ", "16 ต.ค. 09:12")]

X_SCRIPT = [("l", "Hi, is the iPhone 15 still available?", "15:41"),
            ("r", "Yes available, 128GB, 3 months old, bill and box all there", "15:43"),
            ("l", "Final price?", "15:50"),
            ("r", "24500 last price, many buyers waiting, pay fast to book", "15:52"),
            ("l", "Ok sending payment now", "16:04"),
            ("r", "Payment received, will courier today, tracking will share", "16:06")]

TAN = [("l", "陈女士，今天黄金基金又涨了18%！您的账户已经赚了 $9,800。", "10:02"),
       ("r", "真的吗？我想提出来给孙子交学费。", "10:15"),
       ("l", "现在提款要先缴纳15%的税和手续费，把 $60,000 转到香港账户就可以全部提出来。", "10:18"),
       ("l", "这个机会只到星期五。千万不要告诉银行或家人，他们不懂投资。", "10:19"),
       ("r", "好的，我明天去银行转账。", "10:31")]


def render(name, html, w, h):
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={w},{h}",
                    f"--screenshot={OUT / name}", Path(f.name).as_uri()], check=True, capture_output=True)
    Path(f.name).unlink()
    return hashlib.sha256((OUT / name).read_bytes()).hexdigest()


def main():
    render("IN_ravi_notice.png", doc(IN_NOTICE.format(no="4471", date="16-10-2026", ack="31210260045512",
           acct="SCB-0045-118922", holder="RAVI DESHMUKH", utr="628104773215", txdate="11-10-2026", amt="3,600",
           layer="Layer 2 (funds received from first-layer account)", officer="Sunil Patil")), 900, 1180)
    render("IN_x_notice.png", doc(IN_NOTICE.format(no="4502", date="17-10-2026", ack="31210260046108",
           acct="SCB-0045-771034", holder="VIKRAM RATHOD", utr="628144190288", txdate="13-10-2026", amt="24,500",
           layer="Layer 1 (direct victim transfer)", officer="Sunil Patil")), 900, 1180)
    render("TH_nok_notice.png", doc(TH_NOTICE), 900, 1100)
    render("SG_tan_order.png", doc(SG_ORDER), 900, 1050)
    render("IN_ravi_courier.png", doc(COURIER), 900, 700)
    render("IN_ravi_chat.png", chat("Amit Sharma", "ResellKart chat (fictional marketplace)", "#3a77ff",
           "<b>Redmi Note 12, 128GB</b> · ₹3,600 · Kothrud, Pune · Listed 7 Oct 2026", RAVI, "11 Oct 2026"), 420, 820)
    render("TH_nok_chat.png", chat("ณัฐพงษ์ ส.", "Nok Handmade Bags · แชทร้านค้า", "#06c755",
           "<b>กระเป๋าผ้าทอมือ สีน้ำเงิน</b> · 1,250 บาท · โพสต์ 9 ต.ค. 2569", NOK, "15 ต.ค. 2569"), 420, 820)
    x_hash = render("IN_x_chat.png", chat("Meera Joshi", "ResellKart chat (fictional marketplace)", "#3a77ff",
                    "<b>iPhone 15, 128GB</b> · ₹24,500 · Pune · Listed 13 Oct 2026", X_SCRIPT, "13 Oct 2026"), 420, 820)
    render("SG_tan_chat.png", chat("陈导师 Golden Harvest", "WhatsApp-style chat (synthetic)", "#075e54", "", TAN, "15 Oct 2026"), 420, 820)

    # Prior cases at other banks: the same seller script with the item and price swapped. Synthetic.
    banks = ["Sahyadri Cooperative Bank", "Deccan Urban Bank", "Konkan People's Bank"]
    items = [("iPhone 15", "24500"), ("iPhone 14", "21000"), ("PS5 console", "26500"), ("MacBook Air", "38000"),
             ("iPhone 15", "23900"), ("Galaxy S24", "27500"), ("iPhone 13", "18500"), ("Royal Enfield helmet set", "9500"),
             ("iPhone 15 Pro", "41000"), ("Sony A7 camera", "45000"), ("iPhone 14", "20500"), ("iPad Air", "29000"),
             ("iPhone 15", "24000"), ("Galaxy S23", "22000")]
    lib = []
    for k, (item, price) in enumerate(items):
        script = " ".join(t for _, t, _ in X_SCRIPT).replace("iPhone 15", item).replace("24500", price)
        lib.append({"case": f"P-2026-{3100 + k * 7}", "bank": banks[k % 3], "date": f"2026-{8 + k // 6:02d}-{(k * 4) % 27 + 1:02d}",
                    "script": script, "image_sha256": [x_hash] if k in (0, 4, 12) else []})
    lib.append({"case": "P-2026-2990", "bank": banks[1], "date": "2026-07-22", "image_sha256": [],
                "script": "Is the cycle available? Yes, 2 years old, gear working. 4500 ok? Ok come and see tomorrow."})
    (ROOT / "library.json").write_text(json.dumps(lib, indent=1, ensure_ascii=False), encoding="utf-8")
    print("samples and library.json written")


if __name__ == "__main__":
    main()
