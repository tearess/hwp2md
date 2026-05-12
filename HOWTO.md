# HOWTO: HWP/HWPX 파일을 Markdown으로 바꾸는 방법

이 문서는 컴퓨터를 잘 몰라도 따라 할 수 있게 쓴 사용법입니다.

`hwp2md`는 한글 문서 파일인 `.hwp` 또는 `.hwpx`를 읽어서 `.md` 파일로 바꿔 주는 프로그램입니다.

## 1. 이 프로그램이 하는 일

예를 들어 이런 파일이 있다고 해 봅시다.

```text
내문서.hwp
```

이 프로그램을 실행하면 아래 파일들이 생깁니다.

```text
내문서.md
내문서.assets/
내문서.report.json
```

각 파일의 뜻은 이렇습니다.

| 이름 | 뜻 |
| --- | --- |
| `내문서.md` | Markdown으로 바뀐 문서입니다. 보통 이 파일을 열어 보면 됩니다. |
| `내문서.assets/` | 문서 안에 있던 그림 파일들이 들어가는 폴더입니다. |
| `내문서.report.json` | 변환하면서 생긴 알림과 기록입니다. 문제가 있을 때 확인합니다. |

## 2. 준비물

먼저 아래 2개가 필요합니다.

1. Python 3.10 이상
2. 이 프로젝트 폴더

Python이 설치되어 있는지 확인하려면 명령 프롬프트나 PowerShell에서 아래 명령어를 입력합니다.

```powershell
python --version
```

또는 Windows에서 `py` 명령어를 쓰는 경우에는 이렇게 확인합니다.

```powershell
py --version
```

버전이 보이면 준비가 된 것입니다.

## 3. 처음 한 번만 설치하기

이 프로젝트 폴더에서 아래 명령어를 한 번 실행합니다.

```powershell
python -m pip install -r requirements.txt
```

만약 `python` 명령어가 안 되면 아래처럼 해 보세요.

```powershell
py -m pip install -r requirements.txt
```

이 명령어는 `.hwp` 파일을 읽기 위해 필요한 작은 도구를 설치합니다.

## 4. 파일 1개 변환하기

바꾸고 싶은 파일을 프로젝트 폴더에 넣습니다.

예를 들어 파일 이름이 `내문서.hwp`라면 아래처럼 실행합니다.

```powershell
python hwp2md.py 내문서.hwp
```

그러면 같은 폴더에 `내문서.md`가 만들어집니다.

출력 파일 이름을 직접 정하고 싶으면 이렇게 합니다.

```powershell
python hwp2md.py 내문서.hwp -o 결과.md
```

`.hwpx` 파일도 같은 방식입니다.

```powershell
python hwp2md.py 내문서.hwpx -o 결과.md
```

## 5. Windows 배치 파일로 쉽게 실행하기

명령어가 길게 느껴지면 `run_hwp2md.bat`을 사용할 수 있습니다.

```powershell
run_hwp2md.bat 내문서.hwp
```

출력 파일 이름을 정하고 싶으면 이렇게 합니다.

```powershell
run_hwp2md.bat 내문서.hwp 결과.md
```

## 6. 폴더 안의 HWP 파일을 한 번에 변환하기

프로젝트 폴더 안에 있는 `.hwp` 파일을 모두 변환하려면 아래 명령어를 실행합니다.

```powershell
python hwp2md.py --batch "*.hwp" -o converted
```

그러면 `converted` 폴더가 생기고, 그 안에 변환된 `.md` 파일들이 들어갑니다.

`.hwpx` 파일을 모두 변환하려면 이렇게 합니다.

```powershell
python hwp2md.py --batch "*.hwpx" -o converted
```

Windows 배치 파일을 쓰면 더 짧습니다.

```powershell
convert_all_hwp.bat converted
```

이 명령어는 현재 폴더의 `.hwp`와 `.hwpx` 파일을 `converted` 폴더로 변환합니다.

## 7. 변환 결과 보기

변환이 끝나면 `.md` 파일을 열어 보면 됩니다.

예를 들어 아래 파일이 생겼다면:

```text
결과.md
```

이 파일을 VS Code, Obsidian, Typora 같은 Markdown 편집기에서 열 수 있습니다.

그림이 있는 문서는 보통 아래 폴더도 같이 생깁니다.

```text
결과.assets/
```

이 폴더는 지우지 않는 것이 좋습니다. Markdown 문서가 이 폴더 안의 그림을 바라보고 있기 때문입니다.

## 8. 자주 쓰는 명령어 모음

파일 1개 변환:

```powershell
python hwp2md.py 내문서.hwp
```

파일 1개를 원하는 이름으로 변환:

```powershell
python hwp2md.py 내문서.hwp -o 결과.md
```

HWP 파일을 모두 변환:

```powershell
python hwp2md.py --batch "*.hwp" -o converted
```

HWPX 파일을 모두 변환:

```powershell
python hwp2md.py --batch "*.hwpx" -o converted
```

Windows 배치 파일로 파일 1개 변환:

```powershell
run_hwp2md.bat 내문서.hwp
```

Windows 배치 파일로 전체 변환:

```powershell
convert_all_hwp.bat converted
```

## 9. 문제가 생겼을 때

### Python을 찾을 수 없다고 나오는 경우

Python이 설치되어 있지 않거나, 명령어 이름이 다를 수 있습니다.

아래 명령어를 대신 써 보세요.

```powershell
py hwp2md.py 내문서.hwp
```

### `olefile`이 없다고 나오는 경우

아래 명령어를 다시 실행합니다.

```powershell
python -m pip install -r requirements.txt
```

### 그림이 안 보이는 경우

`.md` 파일 옆에 있는 `.assets` 폴더가 같이 있어야 합니다.

예를 들어 `결과.md`가 있다면, 아래 폴더도 함께 있어야 합니다.

```text
결과.assets/
```

### 변환은 됐지만 모양이 원본과 조금 다른 경우

정상일 수 있습니다.

이 프로그램은 HWP 화면을 똑같이 복사하는 도구가 아니라, Markdown으로 읽고 고치기 쉬운 문서를 만드는 도구입니다.

표, 그림, 수식처럼 복잡한 부분은 `결과.report.json` 파일에 변환 기록이 남습니다.

## 웹앱으로 사용하기

명령어로 파일 이름을 입력하는 것이 어렵다면 웹앱으로 사용할 수 있습니다.

프로젝트 폴더에서 아래 명령어를 실행합니다.

```powershell
python webapp.py
```

또는 Windows에서는 배치 파일을 실행해도 됩니다.

```powershell
run_webapp.bat
```

화면에 아래와 비슷한 주소가 보입니다.

```text
http://127.0.0.1:8765
```

브라우저에서 이 주소를 열고 `.hwp` 또는 `.hwpx` 파일을 선택한 뒤 `변환 시작`을 누르면 됩니다.

변환이 끝나면 `hwp2md-result.zip` 파일이 내려받아집니다.

zip 파일 안에는 변환된 Markdown, 그림 폴더, report 파일이 들어 있습니다.

## 10. GitHub에 올릴 때 주의할 점

개인 문서나 샘플 문서가 GitHub에 올라가지 않도록 `.gitignore`가 설정되어 있습니다.

보통 GitHub에는 아래 파일들만 올리면 됩니다.

```text
hwp2md.py
requirements.txt
README.md
HOWTO.md
hwp2md_dev_plan.md
run_hwp2md.bat
run_webapp.bat
webapp.py
convert_all_hwp.bat
tests/
.gitignore
```

아래 파일들은 올리지 않는 것이 좋습니다.

```text
*.hwp
*.hwpx
sample_output/
converted/
*.assets/
*.report.json
```

원본 HWP 문서에는 개인정보나 회사 자료가 들어 있을 수 있으니 조심해야 합니다.
