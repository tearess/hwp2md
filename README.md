# hwp2md

`hwp2md`는 `.hwp`와 `.hwpx` 문서를 Markdown 초안으로 변환하는 로컬 실행형 CLI 도구입니다. **텍스트, 제목/문단 추론, 목록 변환, HWP/HWPX 표 복원, 보도자료 metadata 추출, 이미지/첨부 자산 추출, 변환 리포트 생성, 실패 시 진단 메시지 출력**을 제공합니다. 설계 배경은 [`hwp2md_dev_plan.md`](hwp2md_dev_plan.md)(v0.1.0 설계 문서, 역사 자료)와 [`devlog_hwp2md.md`](devlog_hwp2md.md)에 있습니다.

> 현재 버전은 HWP/HWPX의 화면 배치를 픽셀 단위로 재현하지 않습니다. Markdown으로 자연스럽게 다시 읽고 편집할 수 있는 초안을 만드는 것을 목표로 하며, 보존하지 못한 레이아웃·서식 정보는 `*.report.json`에 기록합니다.

## 설치

Python 3.10 이상을 권장합니다. `.hwpx` 변환은 표준 라이브러리만으로 동작하지만, `.hwp` 변환에는 OLE Compound File을 읽기 위한 `olefile` 패키지가 필요합니다.

```bash
python -m pip install -r requirements.txt
```

패키지로 설치하면 `hwp2md` 명령과 `python -m hwp2md`를 바로 쓸 수 있습니다.

```bash
python -m pip install .
hwp2md --version
```

Windows에서 Python 명령이 `python` 대신 `py`로 등록되어 있다면 다음처럼 실행할 수 있습니다.

```powershell
py -m pip install -r requirements.txt
```

이 작업 폴더에는 독립 가상환경 `.venv`를 만들어 `olefile`을 설치해 두었습니다. 따라서 현재 폴더에서는 별도 전역 설치 없이 다음처럼 실행할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe .\hwp2md.py input.hwp -o output.md
```

## 기본 사용법

```bash
python hwp2md.py input.hwp -o output.md
python hwp2md.py input.hwpx -o docs/output.md --assets-dir docs/output.assets
python hwp2md.py input.hwp --format obsidian --image-mode extract --table-mode gfm
python hwp2md.py input.hwp --source-path none      # frontmatter에 원본 경로를 적지 않음
```

## GUI 프로그램 실행

브라우저를 쓰지 않고 마우스로 파일을 고르는 프로그램 창을 열려면 GUI를 실행합니다.

```powershell
python hwp2md_gui.py
```

Windows에서는 배치 파일을 더블클릭하거나 다음처럼 실행할 수 있습니다.

```powershell
run_gui.bat
```

GUI에서 `파일 추가` 또는 `폴더 추가`를 누르고 `변환 시작`을 누르면 결과가 기본적으로 `converted` 폴더에 생성됩니다.

## Windows 실행 파일 만들기

Python이 없는 PC에서도 쓰도록 GUI를 실행 파일 하나(`hwp2md.exe`)로 만들 수 있습니다.

```powershell
build_exe.bat
```

`dist\hwp2md.exe`가 만들어집니다. 이 파일 하나만 복사하면 어디서든 실행됩니다.

- 결과는 기본적으로 `문서\hwp2md 변환결과` 폴더에 저장됩니다.
- HWP/HWPX 파일이나 폴더를 `hwp2md.exe` 아이콘에 끌어다 놓으면 그 파일이 목록에 들어간 채로 창이 열립니다.
- 실행할 때마다 압축을 풀기 때문에 창이 뜨기까지 2초쯤 걸립니다.
- 서명하지 않은 실행 파일이라 처음 실행할 때 Windows SmartScreen 경고가 나올 수 있습니다. `추가 정보` → `실행`을 누르면 됩니다.

## 웹앱 실행

브라우저에서 파일을 올려 변환하려면 로컬 웹앱을 실행합니다.

```powershell
python webapp.py
```

Windows에서는 배치 파일로도 실행할 수 있습니다.

```powershell
run_webapp.bat
```

기본 주소는 `http://127.0.0.1:8765`입니다. 변환 결과는 Markdown, assets, report를 묶은 zip 파일로 내려받습니다.

변환이 완료되면 기본적으로 다음 파일이 생성됩니다.

| 산출물 | 설명 |
| --- | --- |
| `output.md` | 변환된 Markdown 본문입니다. |
| `output.assets/` | 원본 문서에서 추출한 이미지·첨부 자산 폴더입니다. |
| `output.report.json` | 변환 중 발생한 경고, 손실 항목, metadata, 블록 수, 자산 수를 기록한 리포트입니다. |

`-o`를 지정하지 않는 단건 변환과 `--batch` 일괄 변환은 원본 문서의 파일명을 그대로 사용하고 확장자만 `.md`로 바꿉니다. 예를 들어 `보고서.hwp`는 `보고서.md`로 생성됩니다.

같은 이름의 `.md`가 이미 있으면 덮어쓰지 않고 `보고서-2.md`처럼 번호를 붙여 저장합니다. GUI와 같은 규칙입니다. 덮어쓰려면 `--force`(또는 `--overwrite`)를 지정합니다. 파일 쓰기는 임시 파일에 먼저 쓴 뒤 교체하는 방식이라, 변환 도중에 중단되어도 기존 파일이 잘린 채로 남지 않습니다.

## 일괄 변환

현재 폴더의 모든 HWP 파일을 `converted` 폴더로 변환하려면 다음 명령을 사용합니다.

```bash
python hwp2md.py --batch "*.hwp" -o converted
```

Windows에서는 포함된 배치 파일로 더 간단히 실행할 수 있습니다.

```bat
convert_all_hwp.bat converted
```

HWPX까지 포함하려면 패턴을 나누어 실행합니다.

```bash
python hwp2md.py --batch "*.hwp" -o converted
python hwp2md.py --batch "*.hwpx" -o converted
```

하위 폴더까지 한 번에 변환하려면 재귀 패턴을 씁니다.

```bash
python hwp2md.py --batch "**/*.hwp" -o converted
```

`--batch`에서 `-o`는 출력 폴더입니다. `-o out.md`처럼 파일 경로를 넘기면 폴더 대신 파일이 생기는 사고를 막기 위해 사용 오류로 멈춥니다.

## 종료 코드

| 코드 | 의미 |
| --- | --- |
| `0` | 성공 |
| `1` | 사용 오류(인자·대상 없음, 배치 일부 실패) |
| `2` | 변환 실패(손상·암호화·읽기/쓰기 오류) |
| `130` | 사용자 중단(Ctrl+C) |

## 주요 옵션

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `-o, --output <path>` | 입력 파일명 기반 | 출력 Markdown 경로입니다. 일괄 변환에서는 출력 폴더로 사용합니다. |
| `--assets-dir <path>` | `<output>.assets` | 이미지/첨부 자산을 저장할 폴더입니다. |
| `--format <gfm\|commonmark\|obsidian>` | `gfm` | Markdown 출력 방언입니다. `commonmark`는 표를 항상 HTML로, 각주를 `[1]` + 숫자 목록으로 씁니다. `obsidian`은 그림을 `![[경로]]`로 임베드하고 문장 속 수식을 `$...$`로 쓰며 frontmatter에 `tags`·`aliases`를 넣습니다. |
| `--heading-policy <style\|outline\|font\|none>` | `font` | 제목 추론 정책입니다. `none`이면 모든 텍스트를 일반 문단 중심으로 둡니다. |
| `--table-mode <gfm\|html\|hybrid>` | `hybrid` | 표 후보의 출력 방식입니다. 단순 표는 GFM, 병합 셀이 있는 복잡한 표는 HTML로 출력합니다. |
| `--image-mode <extract\|embed\|skip>` | `extract` | 이미지 처리 방식입니다. `embed`는 아직 별도 구현이 없어 `extract`와 같게 동작하며 실행 시 안내를 출력합니다. |
| `--equation-mode <latex\|image\|both\|skip>` | `latex` | 수식 처리 방식입니다. `image`·`both`는 아직 이미지 수식을 만들지 않아 `latex`와 같게 동작하며 실행 시 안내를 출력합니다. |
| `--no-clean-assets` | 꺼짐 | 재변환 시 이전에 생성된 동일 stem의 자산 파일 정리를 건너뜁니다. |
| `--frontmatter <none\|yaml>` | `yaml` | Markdown 상단 YAML frontmatter 출력 여부입니다. |
| `--source-path <abs\|rel\|none>` | `rel` | frontmatter의 `source`에 적을 원본 경로입니다. `rel`은 출력 폴더 기준 상대 경로이되 폴더 밖으로 나가면 파일명만, `abs`는 절대 경로, `none`은 `source` 줄 생략입니다. |
| `--include-page-artifacts` | 꺼짐 | 결재란·머리말/꼬리말처럼 기본적으로 빼는 페이지 부속 표도 출력에 넣습니다. |
| `--report <path>` | `<output>.report.json` | 변환 리포트 JSON 경로입니다. `--batch`와 함께 쓰면 문서별 리포트는 각 문서 옆에 남기고 이 경로에는 배치 전체 요약을 씁니다. |
| `--batch <glob>` | 없음 | 일괄 변환 패턴입니다. `*/*.hwp`, `**/*.hwp` 같은 재귀 패턴도 풀립니다. |
| `--strict` | 꺼짐 | 경고나 손실 항목이 있으면 실패 처리합니다. |
| `--force, --overwrite` | 꺼짐 | 이미 있는 출력 `.md` 파일을 덮어씁니다. 지정하지 않으면 `이름-2.md`로 저장합니다. |
| `--dump-ir` | 꺼짐 | 개발자용 중간 IR JSON을 함께 출력합니다. |

## 현재 구현 범위와 한계

| 항목 | 상태 | 비고 |
| --- | --- | --- |
| HWP 입력 감지 | 지원 | OLE Compound File signature와 HWP FileHeader를 확인합니다. |
| HWPX 입력 감지 | 지원 | ZIP/XML 구조를 확인합니다. |
| HWPML 입력 | 지원 | 한글 XML 형식(HWPML 2.x, `.hml`)과, 법제처 국가법령정보센터처럼 HWPML을 `.hwp` 확장자로 내려주는 파일을 본문·표·그림까지 변환합니다. |
| 본문 텍스트 추출 | 지원 | HWP `BodyText/Section*`의 `PARA_TEXT` 레코드를 해석하고, inline 제어문자 때문에 빠지는 연도별 수치 등을 보정합니다. |
| 제목/목록 추론 | 부분 지원 | `[1]`, `붙임1`, 짧은 `1. 제목`, `가. 제목` 등은 heading으로 승격합니다. |
| 표 | 부분 지원 | HWP/HWPX 표를 복원합니다. 단순 표는 GFM, 병합 셀이 있는 표는 HTML fallback으로 출력합니다. |
| 보도자료 metadata | 부분 지원 | 상단 보도자료 표의 배포일시, 보도일시, 담당부서, 담당자를 frontmatter와 report에 기록합니다. |
| 이미지/첨부 추출 | 지원 | HWP `BinData/*`, HWPX `BinData/*`를 assets 폴더로 추출합니다. HWP BMP 이미지는 Markdown preview 호환성을 위해 PNG로 변환하고, 공백/괄호가 있는 경로는 안전한 Markdown 링크로 출력합니다. 캡션 뒤 그림 control을 감지하면 해당 위치에 우선 배치합니다. |
| 배포용 문서 | 지원 | 복사·인쇄를 제한한 배포용 HWP는 `ViewText/Section*`의 본문을 풀어 변환하고 report에 `DISTRIBUTION_DOC_DECRYPTED` 경고를 남깁니다. 적법하게 열람·변환할 권한이 있는 문서에만 사용하세요(아래 「배포용 문서(복사·인쇄 제한) 변환 안내」). 암호를 건 문서는 지원하지 않습니다. |
| 변경 추적 문서 | 지원 | 수정 내용을 반영한 최종본(`BodyText`)만 변환하고 삭제 표시된 글자는 넣지 않으며, report에 `TRACK_CHANGES_FINAL_TEXT` 경고를 남깁니다. |
| 하이퍼링크/자동 번호 | 지원 | 웹·메일 하이퍼링크는 `[글자](주소)`로, 그림·표·수식 캡션의 자동 번호는 숫자로 채웁니다. 쪽 번호와 각주 번호는 Markdown에서 의미가 없어 넣지 않습니다. |
| 각주/수식 | 부분 지원 | HWP/HWPX 수식 script(분수·근호·첨자·적분/합·LEFT/RIGHT 괄호·행렬/cases·그리스 문자·화살표 등)를 LaTeX로 옮깁니다. 문단 사이 수식은 `$$ $$`, 문장·표 칸 안 수식은 `\( \)`로 출력합니다. 각주는 placeholder로 보존하고 report에 기록합니다. |
| 변환 리포트 | 지원 | 손실·경고·블록 수·자산 수를 JSON으로 생성합니다. |

## 배포용 문서(복사·인쇄 제한) 변환 안내

한글의 “배포용 문서”로 저장한 `.hwp`는 본문이 `BodyText`가 아니라 암호화된 `ViewText/Section*`에 들어 있습니다. `hwp2md`는 이 본문을 풀어 읽어 Markdown으로 옮기며, 이 동작은 옵션으로 끄거나 켜지 않습니다(배포용 문서와 일반 문서를 같은 명령으로 변환).

이 기능은 **적법하게 열람·변환할 권한이 있는 문서에 한해서만** 사용하세요.

- 배포용 문서의 복사·인쇄 제한은 작성자가 문서의 이용 범위를 정해 둔 표시입니다. 권한 없는 문서에서 이 제한을 우회하거나, 작성자가 허용하지 않은 방식으로 변환 결과를 배포하는 데 이 도구를 쓸 수 없습니다.
- 회사·기관 문서는 내부 보안 정책과 문서 작성자의 허락을 먼저 확인하세요. 열람 권한이 있어도 반출·재배포가 따로 제한될 수 있습니다.
- 변환 실행과 그 결과의 사용은 전적으로 사용자의 판단과 책임입니다. 이 도구는 사용자에게 이용 권한이 있는지 확인하지 않으며, 권한 없이 사용해 생긴 결과에 대해 제작자는 책임지지 않습니다.
- 열기 암호가 걸린 문서는 풀지 않습니다. 한글에서 암호를 입력해 열고 일반 문서로 다시 저장한 뒤 변환하세요.
- 배포용 문서를 변환하면 리포트에 `DISTRIBUTION_DOC_DECRYPTED` 경고가 남습니다. 어떤 문서를 어떤 목적으로 변환했는지 남길 수 있도록 리포트를 함께 보관하는 것을 권합니다.

## 샘플 검증 결과

저장소에는 실제 업무 문서가 들어 있지 않습니다(`.gitignore`의 `*.hwp`, `*.hwpx`). 대신 테스트가 `tests/fixtures.py`로 최소 HWP 바이너리(FileHeader·BodyText/Section·BinData)를 직접 만들어 OLE/HWP 경로를 회귀 검증합니다.

## 테스트

```powershell
python -m unittest          # 표준 라이브러리만으로 실행
python -m ruff check .      # 린트(0건 유지)
```

`tkinter`가 없는 환경에서는 GUI 테스트 6건이 오류 대신 건너뛰기가 됩니다. 표본 `.hwp` 파일이 필요한 1건도 저장소에 표본이 없어 건너뜁니다.

## 라이선스 및 고지

이 프로젝트는 **MIT 라이선스**로 배포합니다. 전문은 [`LICENSE`](LICENSE)에 있습니다.

```
Copyright (c) 2026 tearess (https://github.com/tearess)
```

- 사용·수정·재배포·상업적 이용이 가능합니다. 재배포할 때는 `LICENSE`의 저작권 고지와 허가 문구를 함께 넣어 주세요.
- 런타임 의존성은 `olefile`(BSD 라이선스) 하나이며, 각 패키지는 해당 라이선스를 따릅니다. 변환 결과물에는 원본 문서의 저작권이 함께 걸릴 수 있으므로 원본 문서의 이용 조건은 별도로 확인하세요.
- 본 프로젝트는 한글과컴퓨터와 제휴, 후원, 승인 관계가 없는 독립 도구입니다. “한글”, “한컴”, “HWP”, “HWPX” 등 상표는 각 권리자에게 귀속됩니다.
- 변환 결과는 원본 문서의 모든 레이아웃을 보장하지 않으므로 제출·배포 전 사용자가 반드시 검수해야 합니다.
