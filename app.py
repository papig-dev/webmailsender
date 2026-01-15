from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file, abort
import smtplib
import mimetypes
from email import encoders
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
import json
import os
import re
import sqlite3
from werkzeug.utils import secure_filename
from redis import Redis
from rq import Queue
from datetime import datetime
import uuid

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# 데이터 저장을 위한 디렉토리
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
TEMPLATES_DIR = os.path.join(DATA_DIR, 'templates')
RESULTS_DIR = os.path.join(DATA_DIR, 'results')
DB_FILE = os.path.join(DATA_DIR, 'app.db')
ASSETS_DIR = os.path.join(DATA_DIR, 'assets')

# 디렉토리 생성
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)


def _now_iso() -> str:
    return datetime.now().isoformat()


def _extract_cids_from_html(html: str) -> list[str]:
    if not html:
        return []
    cids = re.findall(r"cid:([a-zA-Z0-9_.-]+)", html)
    seen = set()
    out = []
    for cid in cids:
        if cid not in seen:
            out.append(cid)
            seen.add(cid)
    return out


def _is_valid_cid_key(value: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z0-9_.-]+", value or ''))


def _is_valid_template_id(value: str) -> bool:
    if not value:
        return False
    if '/' in value or '\\' in value or '..' in value:
        return False
    return bool(re.fullmatch(r"[a-zA-Z0-9_.-]+", value))


def _get_template_assets_dir(template_id: str) -> str:
    return os.path.join(ASSETS_DIR, template_id)


def _list_template_assets(template_id: str) -> list[dict]:
    base = _get_template_assets_dir(template_id)
    if not os.path.isdir(base):
        return []
    assets = []
    for fn in sorted(os.listdir(base)):
        p = os.path.join(base, fn)
        if not os.path.isfile(p):
            continue
        cid, _ = os.path.splitext(fn)
        assets.append({'cid': cid, 'filename': fn})
    return assets


def _resolve_inline_images(template_id: str, html: str) -> tuple[dict[str, str], list[str]]:
    cids = _extract_cids_from_html(html)
    inline = {}
    missing = []
    for cid in cids:
        p = _find_inline_image_path(template_id, cid)
        if not p:
            missing.append(cid)
            continue
        inline[cid] = p
    return inline, missing


def _find_inline_image_path(template_id: str, cid: str) -> str | None:
    if not template_id:
        return None
    base = os.path.join(ASSETS_DIR, template_id)
    if not os.path.isdir(base):
        return None

    candidates = []
    direct = os.path.join(base, cid)
    if os.path.isfile(direct):
        candidates.append(direct)

    for fn in os.listdir(base):
        path = os.path.join(base, fn)
        if not os.path.isfile(path):
            continue
        stem, _ = os.path.splitext(fn)
        if stem == cid:
            candidates.append(path)

    if not candidates:
        return None
    candidates.sort(key=lambda p: (0 if os.path.basename(p) == cid else 1, p))
    return candidates[0]


def _attach_inline_image(related_msg: MIMEMultipart, cid: str, file_path: str):
    ctype, _ = mimetypes.guess_type(file_path)
    ctype = ctype or 'application/octet-stream'
    maintype, subtype = ctype.split('/', 1) if '/' in ctype else ('application', 'octet-stream')

    with open(file_path, 'rb') as f:
        data = f.read()

    filename = os.path.basename(file_path)

    if maintype == 'image' and subtype not in ('svg+xml',):
        part = MIMEImage(data, _subtype=subtype)
    else:
        part = MIMEBase(maintype, subtype)
        part.set_payload(data)
        encoders.encode_base64(part)

    part.add_header('Content-ID', f'<{cid}>')
    part.add_header('Content-Disposition', 'inline', filename=filename)
    related_msg.attach(part)


def _html_to_plain_text(html: str) -> str:
    if not html:
        return ''
    text = re.sub(r'<\s*br\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'</p\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()


def build_email_message(subject: str, from_email: str, recipient: str, html: str, template_id: str, strict_inline: bool = True, inline_images: dict[str, str] | None = None):
    related = MIMEMultipart('related')
    related['Subject'] = subject
    related['From'] = from_email
    related['To'] = recipient

    alternative = MIMEMultipart('alternative')
    alternative.attach(MIMEText(_html_to_plain_text(html), 'plain', 'utf-8'))
    alternative.attach(MIMEText(html or '', 'html', 'utf-8'))
    related.attach(alternative)

    if inline_images is None:
        inline_images, missing = _resolve_inline_images(template_id, html)
        if strict_inline and missing:
            raise ValueError('인라인 이미지 파일을 찾을 수 없습니다: ' + ', '.join(missing))

    for cid, p in (inline_images or {}).items():
        _attach_inline_image(related, cid, p)

    return related


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA busy_timeout = 3000')
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def _get_redis_url() -> str:
    return os.environ.get('REDIS_URL', 'redis://localhost:6379/0')


def get_queue() -> Queue:
    qname = os.environ.get('RQ_QUEUE', 'webmailsender')
    conn = Redis.from_url(_get_redis_url())
    return Queue(qname, connection=conn)


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS send_runs (
            id TEXT PRIMARY KEY,
            template_id TEXT,
            template_title TEXT NOT NULL,
            subject TEXT NOT NULL,
            from_email TEXT NOT NULL,
            html_content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            status TEXT NOT NULL,
            total_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            fail_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS send_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            recipient_email TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            sent_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES send_runs(id) ON DELETE CASCADE,
            UNIQUE(run_id, recipient_email)
        );

        CREATE INDEX IF NOT EXISTS idx_send_runs_created_at ON send_runs(created_at);
        CREATE INDEX IF NOT EXISTS idx_send_recipients_run_status ON send_recipients(run_id, status);
        """
    )
    conn.commit()
    conn.close()


def reset_run_for_execution(run_id: str, status: str = 'queued'):
    conn = get_db()
    conn.execute(
        """
        UPDATE send_runs
           SET status = ?,
               started_at = NULL,
               finished_at = NULL
         WHERE id = ?
        """,
        (status, run_id),
    )
    conn.commit()
    conn.close()


def mark_all_recipients_failed(run_id: str, error: str):
    now = _now_iso()
    conn = get_db()
    conn.execute(
        """
        UPDATE send_recipients
           SET status = 'failed',
               attempt_count = attempt_count + 1,
               last_error = ?,
               updated_at = ?
         WHERE run_id = ? AND status IN ('pending', 'failed')
        """,
        (error, now, run_id),
    )
    conn.commit()
    conn.close()


def create_send_run(template_id: str, template: dict, from_email: str, recipients: list[str]) -> str:
    run_id = str(uuid.uuid4())
    now = _now_iso()
    conn = get_db()
    conn.execute(
        """
        INSERT INTO send_runs (
            id, template_id, template_title, subject, from_email, html_content,
            created_at, started_at, status, total_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            template_id,
            template.get('title') or template_id,
            template.get('subject') or '',
            from_email,
            template.get('html_content') or '',
            now,
            None,
            'queued',
            len(recipients),
        ),
    )
    conn.commit()
    conn.close()
    return run_id


def get_run_status(run_id: str) -> str | None:
    conn = get_db()
    row = conn.execute("SELECT status FROM send_runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return row['status']


def set_run_status(run_id: str, status: str, started_at: str | None = None, finished_at: str | None = None):
    conn = get_db()
    conn.execute(
        """
        UPDATE send_runs
           SET status = ?,
               started_at = COALESCE(?, started_at),
               finished_at = COALESCE(?, finished_at)
         WHERE id = ?
        """,
        (status, started_at, finished_at, run_id),
    )
    conn.commit()
    conn.close()


def fetch_run_status_summary(run_id: str) -> dict | None:
    conn = get_db()
    run = conn.execute(
        """
        SELECT id, template_title AS title, created_at, started_at, finished_at, status
          FROM send_runs
         WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if not run:
        conn.close()
        return None

    cur = conn.execute(
        """
        SELECT
          SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS success_count,
          SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS fail_count,
          SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
          COUNT(*) AS total_count
        FROM send_recipients
        WHERE run_id = ?
        """,
        (run_id,),
    )
    counts = cur.fetchone() or {}
    conn.close()

    out = dict(run)
    out['success_count'] = int(counts['success_count'] or 0)
    out['fail_count'] = int(counts['fail_count'] or 0)
    out['pending_count'] = int(counts['pending_count'] or 0)
    out['total_count'] = int(counts['total_count'] or 0)
    out['sent_at'] = out.get('finished_at') or out.get('started_at') or out.get('created_at')
    return out


def background_send_run(run_id: str, retry_only: bool = False):
    detail = fetch_run_detail(run_id)
    if not detail:
        return

    template_id = detail.get('template_id') or ''
    html = detail.get('html_content') or ''
    from_email = detail.get('from_email') or ''
    subject = detail.get('subject') or ''

    inline_images, missing = _resolve_inline_images(template_id, html)
    if missing:
        err = '인라인 이미지 파일을 찾을 수 없습니다: ' + ', '.join(missing)
        mark_all_recipients_failed(run_id, err)
        refresh_run_counts(run_id)
        set_run_status(run_id, 'failed', finished_at=_now_iso())
        return

    status = get_run_status(run_id)
    if status in ('cancel_requested', 'canceled'):
        set_run_status(run_id, 'canceled', finished_at=_now_iso())
        return

    set_run_status(run_id, 'running', started_at=_now_iso())

    config = load_config()

    targets = []
    for row in detail.get('recipient_rows', []):
        st = row.get('status')
        if retry_only:
            if st in ('pending', 'failed'):
                targets.append(row['recipient_email'])
        else:
            targets.append(row['recipient_email'])

    if not targets:
        refresh_run_counts(run_id)
        set_run_status(run_id, 'finished', finished_at=_now_iso())
        return

    try:
        server = smtplib.SMTP(config['smtp_server'], config['smtp_port'])
        if config.get('smtp_user') and config.get('smtp_password'):
            server.starttls()
            server.login(config['smtp_user'], config['smtp_password'])

        for i, recipient in enumerate(targets, start=1):
            if get_run_status(run_id) == 'cancel_requested':
                server.quit()
                refresh_run_counts(run_id)
                set_run_status(run_id, 'canceled', finished_at=_now_iso())
                return

            try:
                msg = build_email_message(
                    subject=subject,
                    from_email=from_email,
                    recipient=recipient,
                    html=html,
                    template_id=template_id,
                    strict_inline=True,
                    inline_images=inline_images,
                )
                server.send_message(msg)
                update_recipient_status(run_id, recipient, 'sent', error=None, sent_at=_now_iso())
            except Exception as e:
                update_recipient_status(run_id, recipient, 'failed', error=str(e), sent_at=None)

            if i % 10 == 0:
                refresh_run_counts(run_id)

        server.quit()
        refresh_run_counts(run_id)
        set_run_status(run_id, 'finished', finished_at=_now_iso())

    except Exception as e:
        err = str(e)
        mark_all_recipients_failed(run_id, err)
        refresh_run_counts(run_id)
        set_run_status(run_id, 'failed', finished_at=_now_iso())


def upsert_run_recipients(run_id: str, recipients: list[str]):
    if not recipients:
        return
    now = _now_iso()
    rows = [(run_id, email, 'pending', now) for email in recipients]
    conn = get_db()
    conn.executemany(
        """
        INSERT INTO send_recipients (run_id, recipient_email, status, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(run_id, recipient_email) DO NOTHING
        """,
        rows,
    )
    conn.commit()
    conn.close()


def update_recipient_status(run_id: str, recipient: str, status: str, error: str | None = None, sent_at: str | None = None):
    now = _now_iso()
    conn = get_db()
    conn.execute(
        """
        UPDATE send_recipients
           SET status = ?,
               attempt_count = attempt_count + 1,
               last_error = ?,
               sent_at = ?,
               updated_at = ?
         WHERE run_id = ? AND recipient_email = ?
        """,
        (status, error, sent_at, now, run_id, recipient),
    )
    conn.commit()
    conn.close()


def refresh_run_counts(run_id: str):
    conn = get_db()
    cur = conn.execute(
        """
        SELECT
          SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS success_count,
          SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS fail_count,
          SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
          COUNT(*) AS total_count
        FROM send_recipients
        WHERE run_id = ?
        """,
        (run_id,),
    )
    row = cur.fetchone() or {}
    success_count = int(row['success_count'] or 0)
    fail_count = int(row['fail_count'] or 0)
    total_count = int(row['total_count'] or 0)

    conn.execute(
        """
        UPDATE send_runs
           SET total_count = ?,
               success_count = ?,
               fail_count = ?
         WHERE id = ?
        """,
        (total_count, success_count, fail_count, run_id),
    )
    conn.commit()
    conn.close()


def mark_run_finished(run_id: str, status: str = 'finished'):
    now = _now_iso()
    conn = get_db()
    conn.execute(
        """
        UPDATE send_runs
           SET finished_at = ?,
               status = ?
         WHERE id = ?
        """,
        (now, status, run_id),
    )
    conn.commit()
    conn.close()


def fetch_run_summaries() -> list[dict]:
    conn = get_db()
    cur = conn.execute(
        """
        SELECT id,
               template_title AS title,
               COALESCE(finished_at, started_at, created_at) AS sent_at,
               total_count,
               success_count,
               fail_count,
               status
          FROM send_runs
         ORDER BY created_at DESC
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def fetch_run_detail(run_id: str) -> dict | None:
    conn = get_db()
    run = conn.execute(
        """
        SELECT id,
               template_id,
               template_title AS title,
               subject,
               from_email,
               html_content,
               created_at,
               started_at,
               finished_at,
               status,
               total_count,
               success_count,
               fail_count
          FROM send_runs
         WHERE id = ?
        """,
        (run_id,),
    ).fetchone()

    if not run:
        conn.close()
        return None

    rec_cur = conn.execute(
        """
        SELECT recipient_email, status, last_error, attempt_count, sent_at
          FROM send_recipients
         WHERE run_id = ?
         ORDER BY id ASC
        """,
        (run_id,),
    )
    recipient_rows = [dict(r) for r in rec_cur.fetchall()]
    conn.close()

    errors = []
    for r in recipient_rows:
        if r.get('status') == 'failed' and r.get('last_error'):
            errors.append(f"{r['recipient_email']}: {r['last_error']}")

    pending_count = sum(1 for r in recipient_rows if r.get('status') == 'pending')

    out = dict(run)
    out['sent_at'] = out.get('finished_at') or out.get('started_at') or out.get('created_at')
    out['recipients'] = [r['recipient_email'] for r in recipient_rows]
    out['recipient_rows'] = recipient_rows
    out['errors'] = errors
    out['pending_count'] = pending_count
    out['can_retry'] = out.get('status') not in ('queued', 'running', 'cancel_requested')
    return out

# 설정 파일
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')

def load_config():
    """SMTP 설정 로드"""
    defaults = {
        'smtp_server': 'smtp.gmail.com',
        'smtp_port': 587,
        'smtp_user': '',
        'smtp_password': '',
        'from_email': '',
        'test_recipient_email': ''
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
            for k, v in defaults.items():
                config.setdefault(k, v)
            return config
        except Exception:
            return defaults
    return defaults

def save_config(config):
    """SMTP 설정 저장"""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = f"{CONFIG_FILE}.tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, CONFIG_FILE)


def parse_email_list(value: str) -> list[str]:
    value = (value or '').strip()
    if not value:
        return []
    parts = re.split(r"[\n,;]+", value)
    return [p.strip() for p in parts if p and p.strip()]


init_db()

def get_template_list():
    """템플릿 목록 가져오기"""
    templates = []
    for filename in os.listdir(TEMPLATES_DIR):
        if filename.endswith('.json'):
            template_id = filename[:-5]
            filepath = os.path.join(TEMPLATES_DIR, filename)
            with open(filepath, 'r', encoding='utf-8') as f:
                template = json.load(f)
                templates.append({
                    'id': template_id,
                    'title': template.get('title', template_id),
                    'created_at': template.get('created_at', ''),
                    'recipients': template.get('recipients', [])
                })
    return templates

def save_template(template_id, title, subject, html_content, recipients, from_email=None):
    """템플릿 저장"""
    template_data = {
        'title': title,
        'subject': subject,
        'html_content': html_content,
        'recipients': recipients,
        'from_email': from_email,
        'created_at': datetime.now().isoformat(),
        'updated_at': datetime.now().isoformat()
    }
    filepath = os.path.join(TEMPLATES_DIR, f'{template_id}.json')
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(template_data, f, ensure_ascii=False, indent=2)

def load_template(template_id):
    """템플릿 로드"""
    filepath = os.path.join(TEMPLATES_DIR, f'{template_id}.json')
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def save_send_result(result_id, title, recipients, success_count, fail_count, errors):
    """발송 결과 저장"""
    result_data = {
        'id': result_id,
        'title': title,
        'recipients': recipients,
        'success_count': success_count,
        'fail_count': fail_count,
        'errors': errors,
        'sent_at': datetime.now().isoformat()
    }
    filepath = os.path.join(RESULTS_DIR, f'{result_id}.json')
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2)

def get_send_results():
    """발송 결과 목록 가져오기"""
    results = []
    for filename in os.listdir(RESULTS_DIR):
        if filename.endswith('.json'):
            result_id = filename[:-5]
            filepath = os.path.join(RESULTS_DIR, filename)
            with open(filepath, 'r', encoding='utf-8') as f:
                result = json.load(f)
                results.append(result)
    # 최신 순으로 정렬
    results.sort(key=lambda x: x['sent_at'], reverse=True)
    return results

@app.route('/')
def index():
    """홈 페이지 - 템플릿 목록"""
    templates = get_template_list()
    return render_template('index.html', templates=templates)

@app.route('/template/new')
def new_template():
    """새 템플릿 생성"""
    return render_template('template_edit.html', template=None)

@app.route('/template/<template_id>')
def edit_template(template_id):
    """템플릿 편집"""
    template = load_template(template_id)
    if not template:
        flash('템플릿을 찾을 수 없습니다.')
        return redirect(url_for('index'))
    return render_template('template_edit.html', template=template, template_id=template_id)

@app.route('/template/save', methods=['POST'])
def save_template_route():
    """템플릿 저장"""
    template_id = request.form.get('template_id') or str(uuid.uuid4())
    title = request.form.get('title')
    subject = request.form.get('subject')
    html_content = request.form.get('html_content')
    recipients_text = request.form.get('recipients', '')
    from_email = request.form.get('from_email', '').strip() or None
    
    # 수신자 목록 파싱
    recipients = [email.strip() for email in recipients_text.split('\n') if email.strip()]
    
    save_template(template_id, title, subject, html_content, recipients, from_email)
    flash('템플릿이 저장되었습니다.')
    return redirect(url_for('index'))

@app.route('/template/<template_id>/send')
def send_page(template_id):
    """메일 발송 페이지"""
    template = load_template(template_id)
    if not template:
        flash('템플릿을 찾을 수 없습니다.')
        return redirect(url_for('index'))
    config = load_config()
    return render_template('send.html', template=template, template_id=template_id, config=config)

@app.route('/send/test', methods=['POST'])
def send_test_email():
    """테스트 메일 발송"""
    template_id = request.form.get('template_id')
    test_email = request.form.get('test_email')
    
    template = load_template(template_id)
    if not template:
        return jsonify({'error': '템플릿을 찾을 수 없습니다.'}), 400

    config = load_config()
    test_emails = parse_email_list(test_email) or parse_email_list(config.get('test_recipient_email'))

    if not test_emails:
        return jsonify({'error': '설정에서 테스트 수신자 이메일을 먼저 입력해주세요.'}), 400
    
    # 메일 발송
    try:
        server = smtplib.SMTP(config['smtp_server'], config['smtp_port'])
        
        # 인증이 필요한 경우에만 로그인
        if config.get('smtp_user') and config.get('smtp_password'):
            server.starttls()
            server.login(config['smtp_user'], config['smtp_password'])
        
        success_count = 0
        fail_count = 0
        errors = []

        inline_images, missing = _resolve_inline_images(template_id, template.get('html_content') or '')
        if missing:
            return jsonify({'error': '인라인 이미지 파일을 찾을 수 없습니다: ' + ', '.join(missing)}), 400

        for recipient in test_emails:
            try:
                from_email = template.get('from_email') or config['from_email']
                test_html = template['html_content']
                test_html = f"""
                <div style="background-color: #f0f0f0; padding: 10px; margin-bottom: 20px; border-left: 4px solid #007bff;">
                    <p style="margin: 0; color: #666;">⚠️ 이것은 테스트 메일입니다. 실제 발송이 아닙니다.</p>
                </div>
                {test_html}
                """

                msg = build_email_message(
                    subject=f"[테스트] {template['subject']}",
                    from_email=from_email,
                    recipient=recipient,
                    html=test_html,
                    template_id=template_id,
                    strict_inline=True,
                    inline_images=inline_images,
                )

                server.send_message(msg)
                success_count += 1
            except Exception as e:
                fail_count += 1
                errors.append(f"{recipient}: {str(e)}")

        server.quit()

        overall_success = fail_count == 0
        return jsonify({
            'success': overall_success,
            'success_count': success_count,
            'fail_count': fail_count,
            'errors': errors,
            'message': f'테스트 메일 발송 완료 (성공 {success_count} / 실패 {fail_count})'
        })
        
    except Exception as e:
        return jsonify({'error': f'메일 발송 실패: {str(e)}'}), 500

@app.route('/send', methods=['POST'])
def send_email():
    """메일 발송"""
    template_id = request.form.get('template_id')
    recipients_text = request.form.get('recipients', '')
    
    template = load_template(template_id)
    if not template:
        return jsonify({'error': '템플릿을 찾을 수 없습니다.'}), 400
    
    config = load_config()
    
    # 수신자 목록 파싱
    recipients = [email.strip() for email in recipients_text.split('\n') if email.strip()]
    
    # 메일 발송(run 단위로 DB 저장)
    from_email = template.get('from_email') or config['from_email']
    run_id = create_send_run(template_id, template, from_email, recipients)
    upsert_run_recipients(run_id, recipients)

    # 미리 검증(즉시 사용자에게 피드백)
    _, missing = _resolve_inline_images(template_id, template.get('html_content') or '')
    if missing:
        err = '인라인 이미지 파일을 찾을 수 없습니다: ' + ', '.join(missing)
        mark_all_recipients_failed(run_id, err)
        refresh_run_counts(run_id)
        set_run_status(run_id, 'failed', finished_at=_now_iso())
        return jsonify({'error': err, 'result_id': run_id}), 400

    # 백그라운드 enqueue
    try:
        q = get_queue()
        q.enqueue('app.background_send_run', run_id)
    except Exception as e:
        err = f'백그라운드 큐 등록 실패: {str(e)}'
        mark_all_recipients_failed(run_id, err)
        refresh_run_counts(run_id)
        set_run_status(run_id, 'failed', finished_at=_now_iso())
        return jsonify({'error': err, 'result_id': run_id}), 500

    return jsonify({'success': True, 'result_id': run_id, 'status': 'queued'})


@app.route('/result/<result_id>/retry', methods=['POST'])
def retry_result(result_id):
    """실패/미발송(pending)만 재발송 (같은 run 내 중복 발송 방지)"""
    detail = fetch_run_detail(result_id)
    if not detail:
        return jsonify({'error': '결과를 찾을 수 없습니다.'}), 404

    if detail.get('status') in ('queued', 'running', 'cancel_requested'):
        return jsonify({'error': '이미 발송 중인 작업입니다.'}), 400

    config = load_config()

    # 재발송 대상: pending/failed
    targets = [r['recipient_email'] for r in detail.get('recipient_rows', []) if r.get('status') in ('pending', 'failed')]
    if not targets:
        return jsonify({'success': True, 'message': '재발송 대상이 없습니다.'})

    template_id = detail.get('template_id') or ''
    inline_images, missing = _resolve_inline_images(template_id, detail.get('html_content') or '')
    if missing:
        return jsonify({'error': '인라인 이미지 파일을 찾을 수 없습니다: ' + ', '.join(missing)}), 400

    reset_run_for_execution(result_id, status='queued')

    try:
        q = get_queue()
        q.enqueue('app.background_send_run', result_id, True)
    except Exception as e:
        set_run_status(result_id, 'failed', finished_at=_now_iso())
        return jsonify({'error': f'백그라운드 큐 등록 실패: {str(e)}'}), 500

    return jsonify({'success': True, 'message': '재발송 작업이 시작되었습니다.', 'result_id': result_id, 'status': 'queued'})


@app.route('/result/<result_id>/status')
def result_status(result_id):
    s = fetch_run_status_summary(result_id)
    if not s:
        return jsonify({'error': '결과를 찾을 수 없습니다.'}), 404
    return jsonify(s)


@app.route('/result/<result_id>/cancel', methods=['POST'])
def cancel_result(result_id):
    st = get_run_status(result_id)
    if not st:
        return jsonify({'error': '결과를 찾을 수 없습니다.'}), 404

    if st not in ('queued', 'running'):
        return jsonify({'error': '현재 상태에서는 취소할 수 없습니다.'}), 400

    set_run_status(result_id, 'cancel_requested')
    return jsonify({'success': True, 'status': 'cancel_requested'})


@app.route('/template/<template_id>/assets')
def template_assets(template_id):
    if not template_id:
        return jsonify({'assets': []})
    return jsonify({'assets': _list_template_assets(template_id)})


@app.route('/template/<template_id>/assets/view/<cid>')
def view_template_asset(template_id, cid):
    if not _is_valid_template_id(template_id) or not _is_valid_cid_key(cid):
        abort(404)

    file_path = _find_inline_image_path(template_id, cid)
    if not file_path:
        abort(404)

    return send_file(file_path)


@app.route('/template/<template_id>/assets/upload', methods=['POST'])
def upload_template_asset(template_id):
    if not template_id:
        return jsonify({'error': '템플릿 ID가 필요합니다.'}), 400

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'error': '파일을 선택해주세요.'}), 400

    cid = (request.form.get('cid') or '').strip()
    safe_name = secure_filename(file.filename)
    stem, ext = os.path.splitext(safe_name)

    if not cid:
        cid = stem

    if not _is_valid_cid_key(cid):
        return jsonify({'error': 'CID 이름은 영문/숫자/._- 만 사용할 수 있습니다.'}), 400

    ext = (ext or '').lower() or '.bin'
    base = _get_template_assets_dir(template_id)
    os.makedirs(base, exist_ok=True)

    final_path = os.path.join(base, f"{cid}{ext}")
    tmp_path = final_path + '.tmp'

    file.save(tmp_path)
    os.replace(tmp_path, final_path)

    return jsonify({'success': True, 'cid': cid, 'filename': os.path.basename(final_path)})


@app.route('/template/<template_id>/assets/delete', methods=['POST'])
def delete_template_asset(template_id):
    if not template_id:
        return jsonify({'error': '템플릿 ID가 필요합니다.'}), 400

    cid = (request.form.get('cid') or '').strip()
    if not _is_valid_cid_key(cid):
        return jsonify({'error': 'CID 값이 올바르지 않습니다.'}), 400

    base = _get_template_assets_dir(template_id)
    if not os.path.isdir(base):
        return jsonify({'error': '이미지 폴더가 없습니다.'}), 404

    deleted = False
    for fn in os.listdir(base):
        p = os.path.join(base, fn)
        if not os.path.isfile(p):
            continue
        stem, _ = os.path.splitext(fn)
        if stem == cid or fn == cid:
            os.remove(p)
            deleted = True
            break

    if not deleted:
        return jsonify({'error': '파일을 찾을 수 없습니다.'}), 404
    return jsonify({'success': True})

@app.route('/results')
def results():
    """발송 결과 목록"""
    db_results = fetch_run_summaries()
    results = db_results if db_results else get_send_results()
    return render_template('results.html', results=results)

@app.route('/result/<result_id>')
def view_result(result_id):
    """발송 결과 상세 보기"""
    detail = fetch_run_detail(result_id)
    if detail:
        return render_template('result_detail.html', result=detail)

    filepath = os.path.join(RESULTS_DIR, f'{result_id}.json')
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            result = json.load(f)

        # 기존 JSON 결과도 동일한 UI 구조로 맞추기
        error_map = {}
        for e in result.get('errors', []):
            if ':' in e:
                k, v = e.split(':', 1)
                error_map[k.strip()] = v.strip()

        recipient_rows = []
        for email in result.get('recipients', []):
            if email in error_map:
                recipient_rows.append({'recipient_email': email, 'status': 'failed', 'last_error': error_map[email], 'attempt_count': 1, 'sent_at': None})
            else:
                recipient_rows.append({'recipient_email': email, 'status': 'sent', 'last_error': None, 'attempt_count': 1, 'sent_at': None})
        result['recipient_rows'] = recipient_rows
        result['pending_count'] = 0
        result['total_count'] = len(result.get('recipients', []))
        result['can_retry'] = False
        return render_template('result_detail.html', result=result)
    flash('결과를 찾을 수 없습니다.')
    return redirect(url_for('results'))

@app.route('/settings')
def settings():
    """설정 페이지"""
    config = load_config()
    return render_template('settings.html', config=config)

@app.route('/settings/save', methods=['POST'])
def save_settings():
    """설정 저장"""
    smtp_port_raw = (request.form.get('smtp_port', '') or '').strip()
    try:
        smtp_port = int(smtp_port_raw) if smtp_port_raw else 587
    except ValueError:
        flash('SMTP 포트 값이 올바르지 않습니다.')
        return redirect(url_for('settings'))

    config = {
        'smtp_server': (request.form.get('smtp_server') or '').strip(),
        'smtp_port': smtp_port,
        'smtp_user': (request.form.get('smtp_user') or '').strip(),
        'smtp_password': request.form.get('smtp_password') or '',
        'from_email': (request.form.get('from_email') or '').strip(),
        'test_recipient_email': request.form.get('test_recipient_email', '').strip()
    }
    try:
        save_config(config)
        flash('설정이 저장되었습니다.')
    except Exception as e:
        flash(f'설정 저장 중 오류가 발생했습니다: {str(e)}')
    return redirect(url_for('settings'))


@app.route('/test-smtp', methods=['POST'])
def test_smtp():
    smtp_server = (request.form.get('smtp_server') or '').strip()
    smtp_port_raw = (request.form.get('smtp_port') or '').strip()
    smtp_user = (request.form.get('smtp_user') or '').strip()
    smtp_password = request.form.get('smtp_password') or ''

    try:
        smtp_port = int(smtp_port_raw) if smtp_port_raw else 587
    except ValueError:
        return jsonify({'success': False, 'error': 'SMTP 포트 값이 올바르지 않습니다.'}), 400

    if not smtp_server:
        return jsonify({'success': False, 'error': 'SMTP 서버를 입력해주세요.'}), 400

    try:
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=6)
        if smtp_user and smtp_password:
            server.starttls()
            server.login(smtp_user, smtp_password)
        server.noop()
        server.quit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    debug = (os.environ.get('FLASK_DEBUG') or '').lower() in ('1', 'true', 'yes', 'on')
    port = int(os.environ.get('PORT') or '5001')
    app.run(debug=debug, host='0.0.0.0', port=port)
