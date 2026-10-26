# Music Info Tools · 음악 정보 도구

아티스트의 음악 정보를 수집하고 CSV를 기준으로 음원 태그와 앨범아트를 입력하는 Windows용 Python 프로그램입니다.
현재 버전은 [VERSION](VERSION), 버전별 변경 사항은 [CHANGELOG.md](CHANGELOG.md)에서 확인할 수 있습니다.

## 실행

Python 3.10 이상이 필요합니다. Windows GUI에는 Tkinter를 사용합니다.

1. `필수패키지_설치.cmd`를 실행합니다.
2. `위키백과_정보_가져오기.cmd` 또는 `정보_가져오기.cmd`에서 아티스트와 저장 위치를 입력합니다.
3. `정보_입력하기.cmd`에서 음원 폴더와 수집한 CSV를 선택합니다.
4. 매칭과 백업 경로를 미리 확인하고 태그를 적용합니다.

세부 옵션과 저장 구조는 [사용안내.md](사용안내.md)를 참고하세요.

## 명령줄

프로그램 폴더에서 실행합니다.

```powershell
python -m pip install -r requirements.txt
python -B fetch_wikipedia_info.py --artist "아티스트명" --root "음악정보" --preview
python -B fetch_wikipedia_info.py --artist "아티스트명" --root "음악정보"
python -B fetch_music_info.py --artist "아티스트명" --root "음악정보"
python -B apply_music_info.py --music "음원 폴더" --csv "음악정보/아티스트명/아티스트명.csv"
```

태그를 저장하려면 마지막 명령에 `--apply`를 추가합니다. 원본 음원 백업은 기본으로 켜져 있습니다.

## 위키백과 기반 수집기

`위키백과_정보_가져오기.cmd`는 위키백과의 아티스트·음반 문서와 트랙 표를 먼저 읽습니다. MusicBrainz는 위키백과에 없는 작곡가·장르·발매일 또는 트랙 목록만 보강하며, 위키백과 값과 다르면 위키백과 값을 유지하고 CSV의 `검토 메모`에 남깁니다. 두 서비스 모두 API 키 없이 사용할 수 있습니다.

CSV의 처음 9개 열은 기존 프로그램과 같고, 그 뒤에 출처 URL·앨범아트 라이선스·검토 메모가 추가됩니다. 모든 위키백과 문서에 완전한 음반 목록이나 트랙 표가 있는 것은 아니므로 먼저 **API 미리보기**로 결과를 확인하는 것을 권장합니다. 동명 아티스트가 있으면 창의 `문서 제목`에 정확한 위키백과 문서 제목을 입력합니다.

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
