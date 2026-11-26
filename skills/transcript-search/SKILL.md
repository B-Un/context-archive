---
name: transcript-search
description: 대화 원본(.jsonl) 검색. 사용자가 "지난번에", "예전에 했던", "전에 정한", "압축 전에" 등 이전 세션이나 압축 전 내용을 가리킬 때, 또는 압축(compaction) 요약에 나온 수치·파일 경로·결정을 계산이나 결과물에 쓰기 전에 원본에서 확인할 때 사용. Use when the user refers to an earlier session or to anything from before a compaction, or before relying on numbers, paths or decisions taken from a compaction summary.
---

# 대화 원본 검색 (transcript-search)

Claude Code는 모든 대화를 `~/.claude/projects/<프로젝트>/<세션ID>.jsonl`에 남긴다. 압축 요약은 이 원본의 축약본이라 수치·경로·결정이 빠지거나 바뀔 수 있다. 이 스킬은 원본에서 필요한 부분만 찾아 확인하는 방법이다.

## 규칙

- 압축 요약에 나온 수치·파일 경로·사용자 결정을 계산이나 결과물에 쓸 때는 요약을 그대로 쓰지 말고 원본에서 확인한다. 안전·금액·납품에 영향을 주는 수치는 반드시 확인한다.
- 사용자가 이전 세션을 가리키면 기억이나 짐작으로 답하지 말고 원본을 검색한다.
- 원본은 통째로 읽지 않는다(수백 MB일 수 있다). 아래 도구로 필요한 부분만 찾는다.
- 원본과 요약이 다르면 원본을 따르고, 다르다는 사실을 사용자에게 알린다.

## 도구

이 스킬 폴더(이 문서의 기본 폴더)에 있는 `transcripts.py`. Python 3.8 이상이면 되고 추가 설치는 없다. 실행 파일 이름은 환경마다 다르다(`python3`, `python`, Windows의 `py -3`). 먼저 되는 것을 쓴다. 압축 직후 안내 메시지에는 이 PC에서 확인된 정확한 명령이 들어 있다.

```bash
# 세션 찾기 (제목, 폴더, 압축 여부)
python3 -I "<스킬폴더>/transcripts.py" list --project 폴더명일부 --max 20

# 검색 (색인 사용, 0.1초 내외. 검색할 때마다 새 기록만 색인에 추가)
python3 -I "<스킬폴더>/transcripts.py" search "검색어" [--session ID앞8자리] [--roles user,summary,tool_result] [--days 30] [--regex] [--full]

# 앞뒤 맥락 보기 (검색 결과의 #번호 기준)
python3 -I "<스킬폴더>/transcripts.py" show ID앞8자리 --from N --to M
```

- `--roles`: `user`, `assistant`, `summary`(압축 요약), `tool_use`, `tool_result`, `meta`
- 결과는 최신순이다. 너무 많으면 `--session`, `--roles`, `--days`로 좁힌다.
- 색인은 `~/.claude/transcript-archive/transcripts.db`에 있다. 처음 검색할 때 한 번 만들어진다(1~2GB 기준 약 15초). 손상되면 `index --rebuild`로 다시 만든다.
- 색인 결과가 의심스러우면 `--scan`으로 원본을 직접 훑어 대조한다(느림).
