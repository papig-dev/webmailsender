# 웹메일 발송 시스템

HTML 이메일 템플릿을 관리하고 다중 수신자에게 메일을 발송하는 웹 기반 시스템입니다.

## 주요 기능

- 📝 HTML 이메일 템플릿 생성 및 편집
- 👁️ 실시간 미리보기 기능
- 👥 다중 수신자 지원
- 📊 발송 결과 추적 및 상세 보기
- ⚙️ SMTP 서버 설정 관리
- 🎨 반응형 웹 인터페이스

## 설치 방법

1. **Python 설치** (Python 3.7 이상 필요)

2. **의존성 설치**
   ```bash
   pip install -r requirements.txt
   ```

3. **애플리케이션 실행**
   ```bash
   python app.py
   ```

4. **웹 브라우저 접속**
   ```
   http://localhost:5001
   ```

## Docker Compose로 실행하기 (권장)

리눅스 환경에서 Docker만으로 `web`(Flask) / `worker`(RQ) / `redis` / `mailhog`를 함께 실행하는 구성이 포함되어 있습니다.

1. **실행**
   ```bash
   docker compose up --build
   ```

2. **웹 UI 접속**
   - `http://localhost:5001`

3. **MailHog UI 접속(개발용 수신함)**
   - `http://localhost:8025`

4. **SMTP 설정(중요)**
   Docker Compose로 실행 중일 때, 설정 화면에서 SMTP 서버는 컨테이너 네트워크 기준으로 입력해야 합니다.
   - **SMTP 서버**: `mailhog`
   - **포트**: `1025`
   - **SMTP 아이디/비밀번호**: 비워두기
   - **발신자 이메일**: 예) `test@localhost`

5. **백그라운드 발송(RQ) 동작 방식**
   - 발송 요청은 즉시 처리되지 않고, `redis` 큐에 적재된 뒤 `worker`가 처리합니다.
   - 발송 결과 상세 화면에서 상태(`queued`/`running`/`finished`/`failed`/`canceled`)와 진행률을 확인할 수 있습니다.
   - 발송 중에는 “발송 취소” 기능으로 중단 요청이 가능합니다.

## 개발 환경에서 MailHog로 테스트하기

MailHog는 개발용 가짜 SMTP 서버로, 실제 메일을 외부로 발송하지 않고 웹 UI에서 수신함을 확인할 수 있습니다.

1. **MailHog 설치/실행**
   ```bash
   brew install mailhog
   mailhog
   ```

2. **웹메일 발송 시스템 SMTP 설정**
   - **SMTP 서버**: `localhost`
   - **포트**: `1025`
   - **SMTP 아이디/비밀번호**: 비워두기
   - **발신자 이메일**: 예) `test@localhost`

3. **MailHog 수신함 확인**
   - 브라우저에서 아래로 접속
     - `http://localhost:8025`

자세한 로컬 SMTP 설정은 `setup_local_smtp.md` 참고.

## 사용 방법

### 1. SMTP 설정
- 상단 메뉴에서 [설정] 클릭
- 사용하는 이메일 서비스의 SMTP 정보 입력
  - Gmail: smtp.gmail.com (포트 587)
  - Naver: smtp.naver.com (포트 587)
  - Daum: smtp.daum.net (포트 465)
- **Gmail 사용 시 앱 비밀번호 발급 필요**
  - [Google 계정 보안](https://myaccount.google.com/security) → 2단계 인증 → 앱 비밀번호

### 2. 템플릿 생성
- [새 템플릿 생성] 버튼 클릭
- 템플릿 제목과 메일 제목 입력
- HTML 내용 편집 (CodeMirror 에디터 지원)
- 원본 보기/미리보기 전환 가능
- 샘플 템플릿 삽입 기능 제공
- 기본 수신자 목록 입력 (선택사항)

### 3. 메일 발송
- 템플릿 목록에서 [발송] 버튼 클릭
- 수신자 목록 확인 및 수정
- 미리보기로 최종 확인
- [메일 발송] 버튼으로 발송 실행
- 실시간 발송 결과 확인

### 3-1. CID 인라인 이미지 사용 (템플릿별 이미지)

CID 인라인 이미지는 HTML 본문에서 `cid:`로 참조되는 이미지를 메일에 첨부하여, 수신자 메일 클라이언트에서 본문 내 이미지로 표시되게 하는 방식입니다.

1. **템플릿 편집 화면에서 이미지 업로드**
   - 템플릿 편집 화면 하단의 CID 이미지 섹션에서 이미지를 업로드합니다.
   - 업로드 시 지정한 CID가 `cid:` 참조 값이 됩니다.

2. **HTML에서 참조**
   - 예시:
     ```html
     <img src="cid:logo" alt="logo" />
     ```

3. **저장 위치/규칙**
   - 업로드된 파일은 아래 위치에 저장됩니다.
     - `data/assets/<template_id>/`
   - 메일 발송 시 `cid:<CID>`로 참조되는 이미지가 없으면 발송 실패로 처리될 수 있습니다.

### 4. 발송 결과 확인
- [발송 결과] 메뉴에서 모든 발송 내역 확인
- 성공/실패 수 및 상세 정보 조회
- 실패한 수신자의 원인 확인

## 파일 구조

```
webmailsender/
├── app.py                 # Flask 애플리케이션 메인 파일
├── requirements.txt       # Python 의존성 목록
├── templates/            # HTML 템플릿 디렉토리
│   ├── base.html         # 기본 레이아웃
│   ├── index.html        # 홈 (템플릿 목록)
│   ├── template_edit.html # 템플릿 편집
│   ├── send.html         # 메일 발송
│   ├── results.html      # 발송 결과 목록
│   ├── result_detail.html # 발송 결과 상세
│   └── settings.html     # 설정 페이지
└── data/                 # 데이터 저장 디렉토리
    ├── config.json       # SMTP 설정
    ├── app.db            # 발송 실행/수신자 상태(SQLite)
    ├── templates/        # 템플릿 데이터
    ├── assets/           # 템플릿별 CID 인라인 이미지
    └── results/          # 발송 결과 데이터
```

## 주요 라이브러리

- **Flask**: 웹 프레임워크
- **CodeMirror**: 코드 에디터 (HTML 편집용)
- **Tabler**: UI 스타일링
- **Font Awesome**: 아이콘

## 발송 결과 저장 방식

- 발송 실행 상태/수신자별 전송 결과는 SQLite(`data/app.db`)에 저장됩니다.
  - `send_runs`: 발송 실행 단위(상태/카운트/시간)
  - `send_recipients`: 수신자별 상태(`pending`/`sent`/`failed`)
- 일부 이전 데이터 호환을 위해 `data/results/*.json` 형식이 남아 있을 수 있습니다.

## 보안 주의사항

- SMTP 비밀번호는 안전하게 저장되지만, 프로덕션 환경에서는 환경 변수 사용 권장
- 앱 비밀번호를 사용하여 계정 보안 유지
- 발송 결과 데이터에는 개인정보가 포함될 수 있으므로 주의

## 확장 기능 제안

- 이메일 템플릿 카테고리 분류
- 예약 발송 기능
- 첨부파일 지원
- 수신자 그룹 관리
- 발송 통계 차트
- 이메일 변수 치환 기능 (예: {이름}, {회사명} 등)

## 라이선스

MIT License
