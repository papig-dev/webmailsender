# macOS 로컬 SMTP 서버 설정 방법

## 방법 1: Postfix 사용 (권장)

### 1. Postfix 활성화
```bash
# Postfix 설정 파일 백업
sudo cp /etc/postfix/main.cf /etc/postfix/main.cf.backup

# Postfix 시작
sudo postfix start

# 부팅 시 자동 시작 설정
sudo launchctl load -w /System/Library/LaunchDaemons/org.postfix.master.plist
```

### 2. 메일 발송 테스트
```bash
echo "Test email body" | mail -s "Test Subject" your-email@example.com
```

### 3. 웹메일 발송 시스템 설정
- **SMTP 서버**: localhost 또는 127.0.0.1
- **포트**: 25
- **SMTP 아이디**: (비워두기)
- **SMTP 비밀번호**: (비워두기)
- **발신자 이메일**: test@localhost 또는 원하는 주소

---

## 방법 2: MailHog 설치 (테스트용)

MailHog는 개발용 가짜 SMTP 서버로, 실제 메일을 보내지 않고 웹에서 확인할 수 있습니다.

### 1. Homebrew로 설치
```bash
brew install mailhog
```

### 2. MailHog 시작
```bash
mailhog
```

### 3. 웹메일 발송 시스템 설정
- **SMTP 서버**: localhost
- **포트**: 1025
- **SMTP 아이디**: (비워두기)
- **SMTP 비밀번호**: (비워두기)
- **발신자 이메일**: test@localhost

### 4. 발송된 메일 확인
- 웹 브라우저에서 http://localhost:8025 접속
- 발송된 모든 메일을 실시간으로 확인 가능

---

## 방법 3: Docker로 Postfix 설정

### 1. docker-compose.yml 생성
```yaml
version: '3'
services:
  postfix:
    image: boky/postfix
    environment:
      - ALLOWED_SENDER_DOMAINS=localhost
      - HOSTNAME=localhost
    ports:
      - "25:587"
```

### 2. 실행
```bash
docker-compose up -d
```

---

## 방법 4: Gmail SMTP 릴레이 설정

로컬에서 Gmail을 통해 메일을 발송하도록 Postfix 설정

### 1. Gmail 앱 비밀번호 발급
1. Google 계정 → 보안 → 2단계 인증
2. 앱 비밀번호 생성 (16자리)

### 2. Postfix 설정
```bash
# 설정 파일 편집
sudo nano /etc/postfix/main.cf
```

아래 내용 추가:
```
relayhost = [smtp.gmail.com]:587
smtp_sasl_auth_enable = yes
smtp_sasl_password_maps = hash:/etc/postfix/sasl_passwd
smtp_sasl_security_options = noanonymous
smtp_tls_security_level = encrypt
smtp_tls_CAfile = /etc/ssl/certs/ca-certificates.crt
```

### 3. 인증 정보 설정
```bash
# 인증 파일 생성
echo "[smtp.gmail.com]:587 your-email@gmail.com:your-app-password" | sudo tee /etc/postfix/sasl_passwd

# 권한 설정
sudo chmod 600 /etc/postfix/sasl_passwd
sudo postmap /etc/postfix/sasl_passwd

# Postfix 재시작
sudo postfix reload
```

---

## 권장 설정

### 개발/테스트 환경: MailHog
- 실제 메일 발송 없이 테스트 가능
- 웹 UI로 발송 내역 즉시 확인

### 프로덕션 환경: Postfix + Gmail 릴레이
- 안정적인 메일 발송
- Gmail의 스팸 필터 우회 가능

## 문제 해결

### Postfix가 실행되지 않을 때
```bash
# 로그 확인
tail -f /var/log/mail.log

# 상태 확인
sudo postfix status
```

### 포트 25가 다른 서비스에서 사용 중일 때
```bash
# 포트 사용 확인
sudo lsof -i :25

# AirPlay Receiver 비활성화 (macOS)
시스템 설정 > 일반 > 공유 > AirPlay 수신기 끄기
```
