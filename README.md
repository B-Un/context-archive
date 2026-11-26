# context-archive

Claude Code 대화가 길어져 **압축(compaction)** 되면 요약만 남고 세부 수치·경로·결정이 빠질 수 있습니다.
이 플러그인은 압축 전 원본과 지난 세션 기록을 빠르게 찾아 확인하게 해 줍니다.

- **압축 직후 안내 (훅)**: 압축이 일어나면 "요약은 축약본이니 원본에서 확인하라"는 안내와 원본 경로가 자동으로 Claude에게 전달됩니다.
- **원본 검색 (스킬)**: "지난번에 했던…", "압축 전에…"라고 하면 Claude가 원본을 검색합니다. 로컬 SQLite 전문검색 색인을 써서 몇 년치 기록도 0.1초 안팎으로 찾습니다(한글 부분 검색 지원).
- **모든 처리는 내 PC 안에서만** 이루어집니다. 외부로 아무것도 보내지 않습니다.

## 설치

Claude Code 터미널 프롬프트에서:

```text
/plugin install context-archive --marketplace B-Un/context-archive
```

마켓플레이스 추가를 물으면 `y`, 설치 범위는 `user`(모든 프로젝트)를 고릅니다.

셸에서 설치할 때:

```bash
claude plugin install context-archive --marketplace B-Un/context-archive
```

데스크톱 앱: 프롬프트 옆 **+** → Plugins → Add marketplace에 `B-Un/context-archive` → Add plugin.

### 필수: 원본 보관 기간 늘리기

Claude Code는 기본적으로 **30일 지난 대화 원본을 지웁니다.** 오래 보관하려면 `~/.claude/settings.json`에 한 줄을 추가하세요(예: 10년).

```json
"cleanupPeriodDays": 3650
```

`0`은 "무제한"이 아니라 "보관 안 함"으로 동작할 수 있으니 쓰지 마세요.

## 요구 사항

- Python 3.8 이상 (`python3`, `python`, `py -3` 중 하나). 추가 패키지는 필요 없습니다.
- Windows에서는 Git for Windows(Git Bash)가 있어야 훅이 실행됩니다. Claude Code 설치 시 보통 함께 쓰입니다.

## 직접 사용하기

```bash
python3 -I <플러그인폴더>/skills/transcript-search/transcripts.py list --max 10
python3 -I <플러그인폴더>/skills/transcript-search/transcripts.py search "검색어" --roles user,summary
python3 -I <플러그인폴더>/skills/transcript-search/transcripts.py show <세션ID앞8자리> --from 10 --to 20
```

색인 위치: `~/.claude/transcript-archive/` (첫 검색 때 자동 생성, 원본 1~2GB 기준 약 15초). 원본이 정리되어 지워져도 색인에 들어간 대화 텍스트는 남습니다. 색인이 손상되면 `index --rebuild`.

## 주의

`~/.claude/transcript-archive/`와 `~/.claude/projects/`에는 본인의 대화 내용이 그대로 들어 있습니다. 다른 사람과 공유하지 마세요.
