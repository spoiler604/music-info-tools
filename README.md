# Music Info Tools · 음악 정보 도구

아티스트의 음악 정보를 수집하고 CSV를 기준으로 음원 태그와 앨범아트를 입력하는 Windows용 Python 프로그램입니다.
현재 버전은 [VERSION](VERSION), 버전별 변경 사항은 [CHANGELOG.md](CHANGELOG.md)에서 확인할 수 있습니다.

## 실행

Python 3.10 이상이 필요합니다. Windows GUI에는 Tkinter를 사용합니다.

1. `필수패키지_설치.cmd`를 실행합니다.
2. `정보_가져오기.cmd`에서 아티스트와 저장 위치를 입력합니다.
3. `정보_입력하기.cmd`에서 음원 폴더와 수집한 CSV를 선택합니다.
4. 매칭과 백업 경로를 미리 확인하고 태그를 적용합니다.

세부 옵션과 저장 구조는 [사용안내.md](사용안내.md)를 참고하세요.

## 명령줄

프로그램 폴더에서 실행합니다.

```powershell
python -m pip install -r requirements.txt
python -B fetch_music_info.py --artist "아티스트명" --root "음악정보"
python -B apply_music_info.py --music "음원 폴더" --csv "음악정보/아티스트명/아티스트명.csv"
```

태그를 저장하려면 마지막 명령에 `--apply`를 추가합니다. 원본 음원 백업은 기본으로 켜져 있습니다.

## 검증

```powershell
python -B -m unittest discover -s tests -v
```

테스트는 고정된 모의 음악 정보와 임시 파일을 사용합니다. 실제 서비스 접속과 음원의 번역·음질 분석은 검사하지 않습니다.

## Git으로 버전 기록

GitHub 플러그인 없이 Git 명령으로 변경 사항과 버전을 기록할 수 있습니다.
커밋 작성자 이름·이메일은 이 저장소의 로컬 설정으로 지정합니다. 개인정보 노출을 줄이려면 GitHub의 noreply 이메일을 사용할 수 있습니다.

```powershell
git status
git diff
git add -- .
git diff --cached --stat
git commit -m "변경한 기능 설명"
```

배포할 때 `VERSION`과 `CHANGELOG.md`를 함께 수정한 뒤 커밋하고 태그를 붙입니다.

```powershell
git tag -a v1.0.0 -m "Release 1.0.0"
git push origin main
git push origin v1.0.0
```

태그 명령은 예시입니다. 이미 존재하는 태그는 다시 만들지 않습니다.
수집 CSV·앨범아트·음원·백업·로컬 인증 파일은 `.gitignore`로 버전 기록에서 제외합니다.
