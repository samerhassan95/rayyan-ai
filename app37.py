import os
import json
import re
import time
import numpy as np
import pandas as pd
import random
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer
import faiss

from google import genai

from flask import Flask
from flask import render_template_string

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

import arabic_reshaper
from bidi.algorithm import get_display
import requests
from flask import request
from threading import Thread
# ==================================
# FLASK
# ==================================
app = Flask(__name__)
latest_report = None


# ==================================
# FONT
# ==================================
pdfmetrics.registerFont(
    TTFont(
        "Arabic",
        "Amiri-Regular.ttf"
    )
)


# ==================================
# GEMINI
# ==================================
GEMINI_API_KEY = "AQ.Ab8RN6L7OZLAwHlka__fcIUIFclDbRZ3r95yuObBZwrYLD-3Fw"
if not GEMINI_API_KEY:
    raise Exception(
        "GEMINI_API_KEY not found"
    )

client = genai.Client(
    api_key=GEMINI_API_KEY
)


# ==================================
# EMBEDDING MODEL
# ==================================
embedding_model = SentenceTransformer(
    "all-MiniLM-L6-v2"
)


def fetch_proposal_from_api(proposal_id):
    BASE_URL = "https://demo.togaar.com/api"
    AI_KEY = "togaar-ai-secret-2025"

    url = f"{BASE_URL}/ai/public-export/{proposal_id}"

    response = requests.get(
        url,
        headers={
            "X-AI-Key": AI_KEY,
            "Accept": "application/json"
        },
        timeout=30
    )

    response.raise_for_status()
    return response.json()


def download_file(url, save_path):
    try:
        headers = {
            "X-AI-Key": "togaar-ai-secret-2025",
            "Accept": "application/pdf",
            "User-Agent": "Mozilla/5.0"
        }

        r = requests.get(
            url,
            headers=headers,
            timeout=60
        )

        print("Downloading:", url)
        print("Status Code:", r.status_code)

        r.raise_for_status()

        with open(save_path, "wb") as f:
            f.write(r.content)

        return save_path

    except Exception as e:
        print("Download error:", url, e)
        return None
    


def prepare_directories():
    os.makedirs("data/company", exist_ok=True)
    os.makedirs("data/proposal", exist_ok=True)



import re

def download_api_documents(api_data, company_path, proposal_path):

    attached = api_data.get("attachedDocuments", {})

    if not attached:
        print("No attached documents found")
        return

    # RFP
    rfp = attached.get("rfpDocument")

    if rfp and rfp.get("fileUrl"):
        if rfp.get("fileUrl"):
            print("Downloading RFP:", rfp["fileUrl"])
            download_file(
                rfp["fileUrl"],
                os.path.join(proposal_path, "rfp.pdf")
    )

    # Company files
    for key, doc in attached.items():

        if not isinstance(doc, dict):
            continue

        file_url = doc.get("fileUrl")
        if not file_url:
            continue

        file_name = doc.get("fileName", f"{key}.pdf")
        file_name = re.sub(r'[^\w\-. ]', '_', file_name)
        file_name = file_name.replace(" ", "_")

        save_path = os.path.join(company_path, file_name)

        download_file(file_url, save_path)



def generate_questions(proposal_text, rag_context):
    
    prompt = f"""
أنت خبير في تحليل RFPs والعروض الفنية.

تخدمهم جداً:
- لا تفكر كمستشار
- لا تشرح
- لا تحلل
- لا تتكلم مع المستخدم
- لا تستخدم أي جمل طبيعية أو مقدمات

أنت تنتج بيانات خام فقط (RAW OUTPUT).



الشروط:
- كل عنصر يحتوي فقط على question
- لا تكتب أي شيء خارج JSON
- لا تكتب أي نص قبل أو بعد JSON
- لا تستخدم أي شرح نهائياً
- لا تبدأ بجمل مثل: "سأقوم" أو "فيما يلي""

بيانات الشركة:
{rag_context}

بيانات المشروع:
{proposal_text[:8000]}

إطار الأسئلة المرجعي:

{QUESTION_FRAMEWORK}

المطلوب:

1- حلل المشروع بالكامل.
2- استنتج أهم الأسئلة المطلوبة لفهم المشروع.
3- استخدم إطار الأسئلة المرجعي كمرجع للتفكير.
4- أضف أسئلة جديدة خاصة بالمشروع من الملفات.
5- جميع الأسئلة يجب أن تكون قابلة للإجابة بـ YES أو NO.
6- لا تكرر أي سؤال.
7- غطِّ الجوانب الفنية والتشغيلية والتجارية والإدارية.
8- أعد من 20 إلى 30 سؤالاً.

IMPORTANT:
- Return ONLY valid JSON
- No markdown
- No explanation

Format:

[
  {{
    "question": "هل توجد أنظمة حالية يجب التكامل معها؟"
  }}
]
"""

    result = call_gemini(prompt)

    print("RAW GEMINI RESULT:\n", result)

    if not result:
        return []

    try:
        result = result.strip()
        result = re.sub(r"```json|```", "", result)

        # استخراج JSON حتى لو فيه كلام زيادة
        match = re.search(r"\[.*\]", result, re.S)
        if match:
            return json.loads(match.group())

        return []

    except Exception as e:
        print("Question Parse Error:", e)
        print("RAW RESULT:", result)
        return []




import re

def safe_json_load(text):
    text = text.strip()
    text = re.sub(r"```json|```", "", text)

    match = re.search(r"\[.*\]", text, re.S)
    if match:
        return json.loads(match.group())

    return []



def parse_markdown_table(rows):
    if not rows:
        return {}

    headers = [h.strip() for h in rows[0].split("|") if h.strip()]

    body = []

    for r in rows[1:]:
        cols = [c.strip() for c in r.split("|") if c.strip()]
        if cols:
            body.append(cols)

    return {
        "headers": headers,
        "rows": body
    }



def parse_section_to_structured(text: str):
    if not text:
        return {
            "paragraphs": [],
            "bullets": [],
            "tables": []
        }

    lines = text.split("\n")

    paragraphs = []
    bullets = []
    tables = []

    current_table = []

    for line in lines:
        line = line.strip()

        # detect table row
        if "|" in line and "---" not in line:
            current_table.append(line)
            continue

        # flush table
        if current_table:
            tables.append(parse_markdown_table(current_table))
            current_table = []

        # bullet points
        if line.startswith("-") or line.startswith("•") or line.startswith("*"):
            bullets.append(line.lstrip("-•* ").strip())

        elif line:
            paragraphs.append(line)

    # flush last table
    if current_table:
        tables.append(parse_markdown_table(current_table))

    return {
        "paragraphs": paragraphs,
        "bullets": bullets,
        "tables": tables
    }

# ==================================
# ARABIC FIX
# ==================================
def fix_arabic(text):

    if not text:
        return ""

    reshaped = arabic_reshaper.reshape(
        str(text)
    )

    return get_display(
        reshaped
    )


# ==================================
# CHUNKING
# ==================================
def chunk_text(
        text,
        size=1200,
        overlap=200
):

    chunks = []

    start = 0

    while start < len(text):

        end = start + size

        chunks.append(
            text[start:end]
        )

        start += (
            size - overlap
        )

    return chunks


# ==================================
# FILE LOADER
# ==================================
def load_file(path):

    try:

        if path.endswith(".pdf"):

            reader = PdfReader(
                path,
                strict=False
            )

            return "".join(
                page.extract_text() or ""
                for page in reader.pages
            )

        elif path.endswith(".xlsx"):

            df = pd.read_excel(path)

            return df.to_string()

        else:

            for enc in [
                "utf-8",
                "cp1256",
                "latin-1"
            ]:

                try:

                    with open(
                        path,
                        "r",
                        encoding=enc
                    ) as f:

                        return f.read()

                except:
                    pass

    except Exception as e:

        print(
            "Load Error:",
            e
        )

    return ""

# ==================================
# VECTOR STORE
# ==================================
class VectorStore:

    def __init__(self):

        self.dimension = 384

        self.index = faiss.IndexFlatIP(
            self.dimension
        )

        self.texts = []

    def add(self, text):

        if not text:
            return

        chunks = chunk_text(
            text,
            size=1200,
            overlap=200
        )

        for chunk in chunks:

            chunk = chunk.strip()

            if len(chunk) < 50:
                continue

            emb = embedding_model.encode(
                chunk,
                normalize_embeddings=True
            ).astype("float32")

            self.index.add(
                np.array([emb])
            )

            self.texts.append(
                chunk
            )

    def search(
            self,
            query,
            k=20
    ):

        if not self.texts:
            return []

        q_emb = embedding_model.encode(
            query,
            normalize_embeddings=True
        ).astype("float32")

        scores, indexes = self.index.search(
            np.array([q_emb]),
            k
        )

        results = []

        for idx in indexes[0]:

            if (
                idx >= 0
                and idx < len(self.texts)
            ):
                results.append(
                    self.texts[idx]
                )

        return results


def build_full_report(api_data=None):

    global latest_report

    print("\n======================")
    print("INITIALIZING RAG")
    print("======================")

    store = VectorStore()

    # 🔥 لو API موجود
    if api_data:
        download_api_documents(
            api_data,
            company_path,
            proposal_path
    )
        
    load_company_documents(company_path, store)
    proposal_text = load_proposal_documents(proposal_path)

    rag_context = build_rag_context(store, proposal_text)

    reference_style = load_reference_style()

    print("\n======================")
    print("GENERATING REPORT")
    print("======================")


   

    latest_report = create_report(
        proposal_text,
        rag_context,
        reference_style
    )
    
    questions = generate_questions(proposal_text, rag_context)
    
    latest_report["Questions"] = questions

    save_json(latest_report, "output.json")
    report_stats(latest_report)



    return latest_report

# ==================================
# LOAD COMPANY DATA
# ==================================
def load_company_documents(
        folder_path,
        store
):

    print(
        "\nLoading company documents..."
    )

    if not os.path.exists(
            folder_path
    ):
        return

    for file in os.listdir(
            folder_path
    ):

        path = os.path.join(
            folder_path,
            file
        )

        print(
            "Company:",
            file
        )

        text = load_file(
            path
        )

        if text:
            store.add(
                text
            )


# ==================================
# LOAD PROPOSAL
# ==================================
def load_proposal_documents(
        folder_path
):

    proposal_text = ""

    print(
        "\nLoading proposal files..."
    )

    if not os.path.exists(
            folder_path
    ):
        return ""

    for file in os.listdir(
            folder_path
    ):

        path = os.path.join(
            folder_path,
            file
        )

        print(
            "Proposal:",
            file
        )

        proposal_text += (
            "\n\n"
            + load_file(path)
        )

    return proposal_text


# ==================================
# BUILD RAG CONTEXT
# ==================================


def build_rag_context(store, proposal_text):

    print("\nBuilding RAG...")

    proposal_text = proposal_text[:5000]

    if not store.texts:
        print("WARNING: RAG EMPTY -> fallback")
        return proposal_text[:8000]

    chunks = store.search(
        proposal_text,
        k=20
    )

    context = "\n\n".join(chunks)

    print("Retrieved Chunks:", len(chunks))
    print("RAG Size =", len(context))

    return context


# ==================================
# GEMINI CALL
# ==================================
def call_gemini(prompt, retries=8):

    for attempt in range(retries):

        try:
            response = client.models.generate_content(
                model="gemini-2.5-pro",
                contents=prompt
            )

            if response and response.text:
                return response.text.strip()

        except Exception as e:

            wait = min(60, (2 ** attempt) + random.uniform(0, 2))

            print(f"Gemini failed attempt {attempt+1}, retry in {wait:.1f}s")

            time.sleep(wait)

    return ""


# ==================================
# CLEAN MARKDOWN
# ==================================
def clean_response(
        text
):

    if not text:
        return ""

    text = re.sub(
        r"```.*?\n",
        "",
        text
    )

    text = text.replace(
        "```",
        ""
    )

    # Remove AI preamble opening paragraph
    # e.g. "بالتأكيد، بصفتي مستشاراً استراتيجياً..."
    preamble_triggers = [
        "بالتأكيد",
        "يسعدني",
        "بكل سرور",
        "بصفتي مستشار",
        "سأقوم بصياغة",
        "تم تصميم هذا القسم",
    ]

    paragraphs = text.strip().split("\n\n")
    if paragraphs:
        first = paragraphs[0].strip()
        if any(trigger in first for trigger in preamble_triggers):
            paragraphs = paragraphs[1:]
        text = "\n\n".join(paragraphs)

    return text.strip()




def send_report_to_api(proposal_id, report, retries=3):
    BASE_URL = "https://demo.togaar.com/api"
    AI_KEY = "togaar-ai-secret-2025"

    url = f"{BASE_URL}/ai/parse/{proposal_id}"

    payload = {
        "parsedData": report
    }

    for attempt in range(retries):
        try:
            response = requests.post(
                url,
                headers={
                    "X-AI-Key": AI_KEY,
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                },
                json=payload,
                timeout=120
            )

            print("Status:", response.status_code)
            print("Response:", response.text)

            response.raise_for_status()
            return response.json()

        except Exception as e:
            print(f"Attempt {attempt+1} failed:", e)
            time.sleep(5)

    return None


def send_progress(proposal_id, step, pct):
    BASE_URL = "https://demo.togaar.com/api"
    AI_KEY = "togaar-ai-secret-2025"
    try:
        requests.post(
            f"{BASE_URL}/ai/progress/{proposal_id}",
            headers={
                "X-AI-Key": AI_KEY,
                "Content-Type": "application/json"
            },
            json={
                "step": step,
                "pct": pct
            },
            timeout=10
        )
        print(f"Sent progress: {step} ({pct}%)")
    except Exception as e:
        print(f"Progress update failed: {e}")


# ==================================
# SECTION GENERATOR
# ==================================
def generate_section(
        section_name,
        proposal_text,
        rag_context,
        reference_style="",
        user_prompt=""
):

    prompt = f"""
أنت كاتب تقارير استشارية محترف.

ممنوع استخدام أي جمل مثل:
- سأقوم
- سنقوم
- بصفتي مستشار
- فيما يلي
- هذا القسم سيقدم

ابدأ مباشرة بالمحتوى بدون مقدمات.

اكتب بأسلوب شركات الاستشارات الكبرى (McKinsey / BCG) لكن بدون الإشارة لنفسك أو استخدام ضمير المتكلم.

المطلوب كتابة قسم واحد فقط:

{section_name}

================================================

تعليمات إلزامية من العميل (يجب الالتزام بها واعتبارها جزءاً من نطاق المشروع):

{user_prompt}

================================================

معلومات الشركة:

{rag_context}

================================================

معلومات المشروع:

{proposal_text[:8000]}

================================================

أسلوب الكتابة المرجعي:

{reference_style}

================================================

قواعد إلزامية:

1- الحد الأدنى 1500 كلمة.

2- لا تقل عن 15 نقطة تفصيلية.

3- لا تقل عن جدولين Markdown.

4- استخدم عناوين فرعية H2 و H3.

5- استخدم أمثلة عملية.

6- استخدم لغة احترافية رسمية.

7- ممنوع الاختصار.

8- ممنوع ترك أي جزء فارغ.

9- اكتب القسم كاملاً وجاهزاً للإدراج في عرض فني.

10- أضف جداول ونقاط تفصيلية كثيرة.

ابدأ الآن.
"""

    result = call_gemini(
        prompt
    )

    return clean_response(
        result
    )



# ==================================
# REPORT SECTIONS
# ==================================
REPORT_SECTIONS = [

    "من نحن",

    "الرؤية",

    "الرسالة",

    "الخدمات",

    "فهمنا للمشروع",

    "نطاق العمل",

    "المنهجية",

    "الخطة الزمنية",

    "الفريق",

    "الملاحق"
]

QUESTION_FRAMEWORK = """
# Project Qualification
هل تم تنفيذ مشروع سابق مشابه للكراسة؟
هل نجح المشروع السابق فنياً؟
هل المشروع ضمن مجال قوة الشركة أم يحتاج شريك تنفيذي؟
هل توجد شروط إلزامية لا نستطيع تحقيقها؟
هل توجد معايير تقييم لا نستطيع استيفاءها؟
هل السجل التجاري يغطي النشاط المطلوب؟
هل لدينا مشروع مشابه بنسبة لا تقل عن 60%؟
من هم المنافسون المحتملون؟
ما سبب خسارتنا المحتمل في هذا العرض؟
هل التقييم اجتياز فني ثم أقل سعر أم تقييم موزون؟
# Business Understanding
- ما الهدف الحقيقي للجهة؟
- ما المشكلة التي تحاول الجهة حلها؟
- هل توجد متطلبات غير مذكورة صراحة؟

# Scope & Deliverables
هل المطلوب استشارة أم تشغيل أم تطوير أم توريد أم تدريب أم دعم؟
هل نطاق العمل يتضمن تشغيل مستمر؟
هل توجد فترة ضمان أو دعم بعد التسليم؟
ما المخرجات المطلوبة وعددها وصيغتها؟
ما المخاطر التي قد تؤدي إلى رفض المخرجات؟
هل مطلوب نقل معرفة رسمي؟
هل يجب توفير مواد تدريبية؟
هل توجد قيود على عدد الصفحات أو المرفقات؟
هل توجد متطلبات تعديل وتطوير حسب الحاجة؟
# Technical Architecture
- هل توجد أنظمة حالية يجب التكامل معها؟
- هل توجد اشتراطات تقنية محددة؟
- هل سيتم توفير API Documentation؟

# Data & AI
من يملك البيانات؟
من يتحمل مسؤولية جودة البيانات؟
هل توجد سياسات بيانات معتمدة؟
هل توجد مبادرات ذكاء اصطناعي قائمة؟
ما مستوى النضج الحالي في جودة البيانات؟
ما الأدوات المستخدمة في ETL/ELT؟
هل توجد متطلبات NDMO؟
هل توجد متطلبات SDAIA؟
هل توجد متطلبات DGA؟
# Cybersecurity & Compliance
- هل توجد متطلبات أمن سيبراني؟
- هل توجد متطلبات امتثال؟

# Team & Resources
هل الفريق المقترح مطابق لمتطلبات الكراسة؟
هل توجد شروط تفرغ؟
هل يتطلب المشروع حضوراً ميدانياً؟
هل يجب أن يكون مدير المشروع في الموقع؟
هل تتوقع الجهة إعادة توظيف الفريق الحالي؟
ما الدور المتوقع من الاستشاري؟
هل الاستشاري مسؤول عن التنفيذ الكامل؟
# Commercial & Cost
- هل توجد تكاليف خفية؟
- هل توجد تراخيص أو استضافة؟

# Project Management
هل نحتاج لجنة توجيهية؟
هل توجد اجتماعات دورية؟
هل الدفعات مرتبطة بمخرجات؟
ما مدة مراجعة الجهة لكل مخرج؟
كيف ستدار طلبات التغيير؟
هل يوجد مورد حالي للجهة؟
هل تم طرح المشروع سابقاً؟
هل توجد مراحل يجب تنفيذها في مقر الجهة؟
# Support & SLA
- هل توجد متطلبات دعم؟
- هل يوجد SLA محدد؟

# Digital Transformation
هل توجد استراتيجية تحول رقمي؟
هل يوجد مكتب تحول رقمي؟
هل يوجد مكتب إدارة مشاريع؟
ما نسبة أتمتة الخدمات؟
ما حجم الأصول التقنية؟
هل يتابع مكتب التحول المشاريع؟

# Business Continuity
هل تم تنفيذ تقييم نضج لاستمرارية الأعمال؟
هل يوجد إطار قائم لاستمرارية الأعمال؟
ما التحديات الحالية؟
هل توجد ارتباطات تنظيمية؟
هل يوجد مركز بيانات احتياطي؟
هل تعتمد الجهة على مزود خدمات خارجي؟


#Business Understanding
ما الهدف الحقيقي للجهة من المشروع؟
ما المشكلة التي تحاول الجهة حلها؟
ما التوجه المستقبلي الذي ترغب الجهة بتحقيقه؟
هل توجد مخرجات غير مذكورة صراحة لكنها متوقعة؟
هل توجد متطلبات ذكرتها الجهة بعد التواصل ولم تذكر في المرفقات؟
هل توجد متطلبات خاصة متوقعة في نطاق المشروع؟
ما البنود الأكثر أهمية في نطاق العمل؟
ما الحد الأدنى للنجاح الفني؟

#Technical Architecture
هل توجد أنظمة حالية يجب التكامل معها؟
هل توجد اشتراطات تقنية محددة؟
ما البنية التقنية الحالية المستخدمة؟
ما الأنظمة الحالية الموجودة؟
كم عدد الأنظمة المتوقع دمجها؟
هل سيتم توفير API Documentation؟
هل سيتم توفير Sandbox؟
هل توجد متطلبات استضافة تفصيلية؟
هل الاستضافة سحابية أم داخلية؟
هل ستكون الاستضافة من مسؤولية الجهة؟


#Cybersecurity & Compliance
هل توجد متطلبات أمن سيبراني؟
هل توجد متطلبات خصوصية بيانات؟
هل توجد متطلبات تصنيف بيانات؟
هل توجد متطلبات ربط حكومي؟
هل يوجد إطار أمني معتمد؟
هل توجد سياسات أمنية للطوارئ؟
هل توجد متطلبات امتثال NCA؟
هل توجد متطلبات امتثال تنظيمية؟


#Commercial & Cost
ما التكاليف الرأسمالية المؤثرة؟
ما البنود التي ستؤثر على التكلفة التشغيلية؟
هل توجد تكاليف تراخيص؟
هل توجد تكاليف استضافة؟
هل توجد تكاليف أجهزة؟
هل توجد تكاليف سفر؟
هل توجد تكاليف دعم فني؟
هل يوجد توريد مخفي داخل النطاق؟
ما التكاليف الخفية؟

#Support & SLA
ما مستوى الخدمة المتوقع SLA؟
هل توجد بلاغات أو مركز خدمة؟
ما متوسط عدد التذاكر الشهرية؟
هل الدعم حضوري أم عن بعد؟
هل الدعم يشمل خارج أوقات العمل؟
هل توجد تقارير دعم شهرية؟
هل توجد سيناريوهات أو حوادث خاصة؟

"""


# ==================================
# SAFE GENERATE
# ==================================
def safe_generate_section(
        section_name,
        proposal_text,
        rag_context,
        reference_style="",
        user_prompt=""
):
    try:
        print(f"\nGenerating: {section_name}")

        result = generate_section(
            section_name,
            proposal_text,
            rag_context,
            reference_style,
            user_prompt
        )

        if not result:
            return f"لم يتم توليد قسم {section_name}"

        return result

    except Exception as e:
        print(f"Section Error ({section_name})", e)
        return f"حدث خطأ أثناء إنشاء {section_name}"

# ==================================
# REPORT CREATOR
# ==================================
def create_report(
        proposal_text,
        rag_context,
        reference_style="",
        user_prompt="",
        proposal_id=None
):

    report = {}

    total = len(
        REPORT_SECTIONS
    )

    current = 1

    for section in REPORT_SECTIONS:

        print(
            f"\n[{current}/{total}] "
            f"{section}"
        )

        if proposal_id:
            send_progress(proposal_id, f"Writing section: {section}", 40 + int((current / total) * 45))

        raw = safe_generate_section(
            section,
            proposal_text,
            rag_context,
            reference_style,
            user_prompt
)
        report[section] = parse_section_to_structured(raw)
        

        current += 1

    return report


# ==================================
# SAVE JSON
# ==================================
def save_json(
        report,
        filename="output.json"
):

    try:

        with open(
            filename,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                report,
                f,
                ensure_ascii=False,
                indent=4
            )

        print(
            f"JSON Saved -> {filename}"
        )

    except Exception as e:

        print(
            "JSON Save Error:",
            e
        )


# ==================================
# LOAD REFERENCE STYLE
# ==================================
def load_reference_style():

    possible_files = [

        "reference_style.txt",

        "reference.txt",

        "style.txt"
    ]

    for file in possible_files:

        if os.path.exists(file):

            try:

                with open(
                    file,
                    "r",
                    encoding="utf-8"
                ) as f:

                    return f.read()

            except:
                pass

    return ""


# ==================================
# REPORT STATISTICS
# ==================================
def report_stats(
        report
):

    print(
        "\n======================"
    )

    print(
        "REPORT STATISTICS"
    )

    print(
        "======================"
    )

    total_words = 0

    for key, value in report.items():

        words = len(
            str(value).split()
        )

        total_words += words

        print(
            f"{key} -> {words} words"
        )

    print(
        "\nTOTAL WORDS:",
        total_words
    )

    print(
        "======================"
    )


# ==================================
# BUILD REPORT
# ==================================
# def build_full_report():

#     global latest_report

#     print(
#         "\n======================"
#     )

#     print(
#         "INITIALIZING RAG"
#     )

#     print(
#         "======================"
#     )

#     store = VectorStore()

#     load_company_documents(
#         "data/company",
#         store
#     )

#     proposal_text = (
#         load_proposal_documents(
#             "data/proposal"
#         )
#     )

#     rag_context = (
#         build_rag_context(
#             store,
#             proposal_text
#         )
#     )

#     reference_style = (
#         load_reference_style()
#     )

#     print(
#         "\n======================"
#     )

#     print(
#         "GENERATING REPORT"
#     )

#     print(
#         "======================"
#     )

#     latest_report = (
#         create_report(
#             proposal_text,
#             rag_context,
#             reference_style
#         )
#     )

#     save_json(
#         latest_report,
#         "output.json"
#     )

#     report_stats(
#         latest_report
#     )

#     return latest_report


def fetch_pending_proposals():
    BASE_URL = "https://demo.togaar.com/api"
    AI_KEY = "togaar-ai-secret-2025"

    url = f"{BASE_URL}/ai/pending-proposals"

    response = requests.get(
        url,
        headers={
            "X-AI-Key": AI_KEY,
            "Accept": "application/json"
        },
        timeout=30
    )

    response.raise_for_status()
    return response.json().get("proposals", [])




def prepare_id_folders(proposal_id):
    base = f"data/{proposal_id}"

    company = os.path.join(base, "company")
    proposal = os.path.join(base, "proposal")

    os.makedirs(company, exist_ok=True)
    os.makedirs(proposal, exist_ok=True)

    return base, company, proposal


# ==================================
# PDF HELPERS
# ==================================
def split_text(
        text,
        max_chars=90
):

    words = str(text).split()

    lines = []

    current = ""

    for word in words:

        test = current + " " + word

        if len(test) <= max_chars:

            current = test

        else:

            lines.append(
                current.strip()
            )

            current = word

    if current:

        lines.append(
            current.strip()
        )

    return lines


# ==================================
# PDF GENERATOR
# ==================================
def generate_pdf(
        report,
        filename="output.pdf"
):

    c = canvas.Canvas(
        filename,
        pagesize=A4
    )

    width, height = A4

    margin_right = 40

    y = height - 50

    # -----------------------
    # Cover Page
    # -----------------------
    c.setFont(
        "Arabic",
        22
    )

    c.drawRightString(
        width - margin_right,
        y,
        fix_arabic(
            "العرض الفني والتجاري"
        )
    )

    y -= 50

    c.setFont(
        "Arabic",
        14
    )

    c.drawRightString(
        width - margin_right,
        y,
        fix_arabic(
            "Generated Automatically"
        )
    )

    c.showPage()

    y = height - 50

    # -----------------------
    # Content
    # -----------------------
    for section_name, section_text in report.items():

        if y < 120:

            c.showPage()

            y = height - 50

        # Section Title
        c.setFont(
            "Arabic",
            16
        )

        c.drawRightString(
            width - margin_right,
            y,
            fix_arabic(
                section_name
            )
        )

        y -= 25

        c.setFont(
            "Arabic",
            10
        )

        text = (
            str(section_text)
            .replace("\r", " ")
            .replace("\t", " ")
        )

        lines = split_text(
            text,
            max_chars=85
        )

        for line in lines:

            if y < 60:

                c.showPage()

                y = height - 50

                c.setFont(
                    "Arabic",
                    10
                )

            c.drawRightString(
                width - 50,
                y,
                fix_arabic(
                    line
                )
            )

            y -= 14

        y -= 15

    c.save()

    print(
        f"PDF Saved -> {filename}"
    )


# ==================================
# EXPORT ALL
# ==================================
def export_report():

    global latest_report

    if not latest_report:

        print(
            "No report generated."
        )

        return

    save_json(
        latest_report,
        "output.json"
    )

    generate_pdf(
        latest_report,
        "output.pdf"
    )


# ==================================
# FLASK UI
# ==================================
@app.route("/")
@app.route("/")
def home():

    global latest_report

    if not latest_report:
        return "<h2>No report generated yet</h2>"

    def render_content(text):

        text = str(text)

        # فصل الجداول Markdown
        parts = text.split("\n")

        html = ""
        table_mode = False
        table_rows = []

        for line in parts:

            line = line.strip()

            # اكتشاف جدول Markdown
            if "|" in line and "---" not in line:

                table_mode = True
                table_rows.append(line)
                continue

            else:

                # لو كنا داخل جدول وخلص
                if table_mode and table_rows:

                    html += convert_table(table_rows)
                    table_rows = []
                    table_mode = False

                # نص عادي
                if line:
                    html += f"<p style='margin:6px 0'>{line}</p>"

        # آخر جدول لو موجود
        if table_rows:
            html += convert_table(table_rows)

        return html


    def convert_table(rows):

        headers = [h.strip() for h in rows[0].split("|") if h.strip()]

        body = rows[1:]

        html = """
        <table class="table">
        <thead>
        <tr>
        """

        for h in headers:
            html += f"<th>{h}</th>"

        html += "</tr></thead><tbody>"

        for row in body:

            cols = [c.strip() for c in row.split("|") if c.strip()]

            html += "<tr>"

            for c in cols:
                html += f"<td>{c}</td>"

            html += "</tr>"

        html += "</tbody></table>"

        return html


    html = """
    <html dir="rtl">

    <head>
    <meta charset="utf-8">

    <style>

    body{
        background:#f5f5f5;
        font-family:Tahoma;
        padding:20px;
    }

    .card{
        background:white;
        margin-bottom:20px;
        padding:20px;
        border-radius:12px;
        box-shadow:0px 0px 10px #ddd;
        page-break-inside: avoid;
    }

    h1{
        text-align:center;
        margin-bottom:30px;
    }

    h2{
        color:#0a4f8f;
        margin-bottom:10px;
    }

    p{
        margin:6px 0;
        line-height:1.8;
    }

    .table{
        width:100%;
        border-collapse:collapse;
        margin:10px 0;
        background:#fff;
    }

    .table th{
        background:#0a4f8f;
        color:white;
        padding:10px;
        border:1px solid #ddd;
    }

    .table td{
        border:1px solid #ddd;
        padding:8px;
        text-align:center;
    }

    .table tr:nth-child(even){
        background:#f9f9f9;
    }

    </style>

    </head>

    <body>

    <h1>Generated Report</h1>

    {% for k,v in data.items() %}

        <div class="card">

            <h2>{{k}}</h2>

            <div>
                {{ render_content(v)|safe }}
            </div>

        </div>

    {% endfor %}

    </body>
    </html>
    """

    return render_template_string(
        html,
        data=latest_report,
        render_content=render_content
    )


@app.route(
    "/webhook/proposal-updated",
    methods=["POST"]
)
def proposal_updated():

    data = request.get_json()

    if not data:
        return {
            "status": "error",
            "message": "No JSON received"
        }, 400

    proposal_id = data.get("proposalId")

    if not proposal_id:
        return {
            "status": "error",
            "message": "proposalId is required"
        }, 400

    Thread(
        target=run_pipeline,
        args=(proposal_id,)
    ).start()

    return {
        "status": "accepted",
        "proposalId": proposal_id
    }, 202


# ==================================
# RUN PIPELINE
# ==================================

def run_pipeline(proposal_id):
    global latest_report

    print("\n=========================")
    print("STARTING PIPELINE:", proposal_id)
    print("=========================\n")

    send_progress(proposal_id, "Fetching proposal data", 10)
    base_path, company_path, proposal_path = prepare_id_folders(proposal_id)

    api_data = fetch_proposal_from_api(proposal_id)

    user_prompt = (
    api_data.get("proposalData", {})
            .get("aiPrompt", "")
)

    if not api_data:
        print("No API data found")
        return

    send_progress(proposal_id, "Downloading documents", 20)
    download_api_documents(
        api_data,
        company_path,
        proposal_path
    )

    send_progress(proposal_id, "Loading context", 30)
    store = VectorStore()

    load_company_documents(company_path, store)

    proposal_text = load_proposal_documents(proposal_path)

    rag_context = build_rag_context(store, proposal_text)

    reference_style = load_reference_style()

    latest_report = create_report(
        proposal_text,
        rag_context,
        reference_style,
        user_prompt=user_prompt,
        proposal_id=proposal_id
)

    output_path = os.path.join(base_path, "output.json")
    

    send_progress(proposal_id, "Generating questions", 90)
    questions = generate_questions(proposal_text, rag_context)
    latest_report["Questions"] = questions
    save_json(latest_report, output_path)
    
    send_progress(proposal_id, "Uploading report", 95)
    send_report_to_api(proposal_id, latest_report)





    
def run_all_pending():
    proposals = fetch_pending_proposals()

    print(f"\nFOUND {len(proposals)} PROPOSALS\n")

    for p in proposals:
        proposal_id = p["proposalId"]

        try:
            run_pipeline(proposal_id)

        except Exception as e:
            print("FAILED:", proposal_id, e)


# ==================================
# MAIN
# ==================================
if __name__ == "__main__":

    run_all_pending()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )

