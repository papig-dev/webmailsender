from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import json
import os
import re
import sqlite3
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

# 디렉토리 생성
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)


def _now_iso() -> str:
    return datetime.now().isoformat()


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


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
            now,
            'running',
            len(recipients),
        ),
    )
    conn.commit()
    conn.close()
    return run_id


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
    out['can_retry'] = True
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

        for recipient in test_emails:
            try:
                msg = MIMEMultipart('alternative')
                msg['Subject'] = f"[테스트] {template['subject']}"
                msg['From'] = template.get('from_email') or config['from_email']
                msg['To'] = recipient

                test_html = template['html_content']
                test_html = f"""
                <div style="background-color: #f0f0f0; padding: 10px; margin-bottom: 20px; border-left: 4px solid #007bff;">
                    <p style="margin: 0; color: #666;">⚠️ 이것은 테스트 메일입니다. 실제 발송이 아닙니다.</p>
                </div>
                {test_html}
                """

                html_part = MIMEText(test_html, 'html', 'utf-8')
                msg.attach(html_part)
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

    success_count = 0
    fail_count = 0
    errors = []

    try:
        server = smtplib.SMTP(config['smtp_server'], config['smtp_port'])

        # 인증이 필요한 경우에만 로그인
        if config.get('smtp_user') and config.get('smtp_password'):
            server.starttls()
            server.login(config['smtp_user'], config['smtp_password'])

        for recipient in recipients:
            try:
                msg = MIMEMultipart('alternative')
                msg['Subject'] = template['subject']
                msg['From'] = from_email
                msg['To'] = recipient

                html_part = MIMEText(template['html_content'], 'html', 'utf-8')
                msg.attach(html_part)

                server.send_message(msg)
                success_count += 1
                update_recipient_status(run_id, recipient, 'sent', error=None, sent_at=_now_iso())
            except Exception as e:
                fail_count += 1
                err = str(e)
                errors.append(f"{recipient}: {err}")
                update_recipient_status(run_id, recipient, 'failed', error=err, sent_at=None)

        server.quit()

        refresh_run_counts(run_id)
        mark_run_finished(run_id, status='finished')

        return jsonify({
            'success': True,
            'result_id': run_id,
            'success_count': success_count,
            'fail_count': fail_count,
            'errors': errors
        })

    except Exception as e:
        refresh_run_counts(run_id)
        mark_run_finished(run_id, status='failed')
        return jsonify({'error': f'메일 발송 실패: {str(e)}', 'result_id': run_id}), 500


@app.route('/result/<result_id>/retry', methods=['POST'])
def retry_result(result_id):
    """실패/미발송(pending)만 재발송 (같은 run 내 중복 발송 방지)"""
    detail = fetch_run_detail(result_id)
    if not detail:
        return jsonify({'error': '결과를 찾을 수 없습니다.'}), 404

    config = load_config()

    # 재발송 대상: pending/failed
    targets = [r['recipient_email'] for r in detail.get('recipient_rows', []) if r.get('status') in ('pending', 'failed')]
    if not targets:
        return jsonify({'success': True, 'message': '재발송 대상이 없습니다.'})

    # run 상태 갱신
    conn = get_db()
    conn.execute(
        """UPDATE send_runs SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ?""",
        ('running', _now_iso(), result_id),
    )
    conn.commit()
    conn.close()

    success_count = 0
    fail_count = 0
    errors = []

    try:
        server = smtplib.SMTP(config['smtp_server'], config['smtp_port'])
        if config.get('smtp_user') and config.get('smtp_password'):
            server.starttls()
            server.login(config['smtp_user'], config['smtp_password'])

        for recipient in targets:
            try:
                msg = MIMEMultipart('alternative')
                msg['Subject'] = detail['subject']
                msg['From'] = detail['from_email']
                msg['To'] = recipient

                html_part = MIMEText(detail['html_content'], 'html', 'utf-8')
                msg.attach(html_part)

                server.send_message(msg)
                success_count += 1
                update_recipient_status(result_id, recipient, 'sent', error=None, sent_at=_now_iso())
            except Exception as e:
                fail_count += 1
                err = str(e)
                errors.append(f"{recipient}: {err}")
                update_recipient_status(result_id, recipient, 'failed', error=err, sent_at=None)

        server.quit()

        refresh_run_counts(result_id)
        mark_run_finished(result_id, status='finished')

        return jsonify({
            'success': True,
            'result_id': result_id,
            'success_count': success_count,
            'fail_count': fail_count,
            'errors': errors,
            'message': f'재발송 완료 (성공 {success_count} / 실패 {fail_count})'
        })
    except Exception as e:
        refresh_run_counts(result_id)
        mark_run_finished(result_id, status='failed')
        return jsonify({'error': f'재발송 실패: {str(e)}'}), 500

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

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
