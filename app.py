from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import json
import os
import re
from datetime import datetime
import uuid

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# 데이터 저장을 위한 디렉토리
DATA_DIR = 'data'
TEMPLATES_DIR = os.path.join(DATA_DIR, 'templates')
RESULTS_DIR = os.path.join(DATA_DIR, 'results')

# 디렉토리 생성
os.makedirs(TEMPLATES_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)

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
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            for k, v in defaults.items():
                config.setdefault(k, v)
            return config
    return defaults

def save_config(config):
    """SMTP 설정 저장"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def parse_email_list(value: str) -> list[str]:
    value = (value or '').strip()
    if not value:
        return []
    parts = re.split(r"[\n,;]+", value)
    return [p.strip() for p in parts if p and p.strip()]

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
    
    # 메일 발송
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
                msg['From'] = template.get('from_email') or config['from_email']
                msg['To'] = recipient
                
                html_part = MIMEText(template['html_content'], 'html', 'utf-8')
                msg.attach(html_part)
                
                server.send_message(msg)
                success_count += 1
            except Exception as e:
                fail_count += 1
                errors.append(f"{recipient}: {str(e)}")
        
        server.quit()
        
        # 결과 저장
        result_id = str(uuid.uuid4())
        save_send_result(result_id, template['title'], recipients, success_count, fail_count, errors)
        
        return jsonify({
            'success': True,
            'result_id': result_id,
            'success_count': success_count,
            'fail_count': fail_count,
            'errors': errors
        })
        
    except Exception as e:
        return jsonify({'error': f'메일 발송 실패: {str(e)}'}), 500

@app.route('/results')
def results():
    """발송 결과 목록"""
    results = get_send_results()
    return render_template('results.html', results=results)

@app.route('/result/<result_id>')
def view_result(result_id):
    """발송 결과 상세 보기"""
    filepath = os.path.join(RESULTS_DIR, f'{result_id}.json')
    if os.path.exists(filepath):
        with open(filepath, 'r', encoding='utf-8') as f:
            result = json.load(f)
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
    config = {
        'smtp_server': request.form.get('smtp_server'),
        'smtp_port': int(request.form.get('smtp_port', 587)),
        'smtp_user': request.form.get('smtp_user'),
        'smtp_password': request.form.get('smtp_password'),
        'from_email': request.form.get('from_email'),
        'test_recipient_email': request.form.get('test_recipient_email', '').strip()
    }
    save_config(config)
    flash('설정이 저장되었습니다.')
    return redirect(url_for('settings'))

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
