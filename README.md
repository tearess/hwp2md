# hwp2md

`hwp2md`는 `.hwp`와 `.hwpx` 문서를 Markdown 초안으로 변환하는 로컬 실행형 CLI 도구입니다. 이 구현은 `hwp2md_dev_plan.md`의 v0.1.0 목표에 맞춰 **텍스트, 제목/문단 추론, 목록 변환, HWP/HWPX 표 복원, 보도자료 metadata 추출, 이미지/첨부 자산 추출, 변환 리포트 생성, 실패 시 진단 메시지 출력**을 우선 제공합니다.

> 현재 버전은 HWP/HWPX의 화면 배치를 픽셀 단위로 재현하지 않습니다. Markdown으로 자연스럽게 다시 읽고 편집할 수 있는 초안을 만드는 것을 목표로 하며, 보존하지 못한 레이아웃·서식 정보는 `*.report.json`에 기록합니다.

## 설치

Python 3.10 이상을 권장합니다. `.hwpx` 변환은 표준 라이브러리만으로 동작하지만, `.hwp` 변환에는 OLE Compound File을 읽기 위한 `olefile` 패키지가 필요합니다.

```bash
python -m pip install -r requirements.txt
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
```

변환이 완료되면 기본적으로 다음 파일이 생성됩니다.

| 산출물 | 설명 |
| --- | --- |
| `output.md` | 변환된 Markdown 본문입니다. |
| `output.assets/` | 원본 문서에서 추출한 이미지·첨부 자산 폴더입니다. |
| `output.report.json` | 변환 중 발생한 경고, 손실 항목, metadata, 블록 수, 자산 수를 기록한 리포트입니다. |

`-o`를 지정하지 않는 단건 변환과 `--batch` 일괄 변환은 원본 문서의 파일명을 그대로 사용하고 확장자만 `.md`로 바꿉니다. 예를 들어 `보고서.hwp`는 `보고서.md`로 생성됩니다.

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

## 주요 옵션

| 옵션 | 기본값 | 설명 |
| --- | --- | --- |
| `-o, --output <path>` | 입력 파일명 기반 | 출력 Markdown 경로입니다. 일괄 변환에서는 출력 폴더로 사용합니다. |
| `--assets-dir <path>` | `<output>.assets` | 이미지/첨부 자산을 저장할 폴더입니다. |
| `--format <gfm\|commonmark\|obsidian>` | `gfm` | Markdown 출력 방언입니다. 현재 v0.1.0에서는 GFM 중심으로 렌더링합니다. |
| `--heading-policy <style\|outline\|font\|none>` | `font` | 제목 추론 정책입니다. `none`이면 모든 텍스트를 일반 문단 중심으로 둡니다. |
| `--table-mode <gfm\|html\|hybrid>` | `hybrid` | 표 후보의 출력 방식입니다. 단순 표는 GFM, 병합 셀이 있는 복잡한 표는 HTML로 출력합니다. |
| `--image-mode <extract\|embed\|skip>` | `extract` | 이미지 처리 방식입니다. `embed`는 현재 `extract`와 동일하게 동작합니다. |
| `--no-clean-assets` | 꺼짐 | 재변환 시 이전에 생성된 동일 stem의 자산 파일 정리를 건너뜁니다. |
| `--frontmatter <none\|yaml>` | `yaml` | Markdown 상단 YAML frontmatter 출력 여부입니다. |
| `--report <path>` | `<output>.report.json` | 변환 리포트 JSON 경로입니다. |
| `--strict` | 꺼짐 | 경고나 손실 항목이 있으면 실패 처리합니다. |
| `--dump-ir` | 꺼짐 | 개발자용 중간 IR JSON을 함께 출력합니다. |

## 현재 구현 범위와 한계

| 항목 | 상태 | 비고 |
| --- | --- | --- |
| HWP 입력 감지 | 지원 | OLE Compound File signature와 HWP FileHeader를 확인합니다. |
| HWPX 입력 감지 | 지원 | ZIP/XML 구조를 확인합니다. |
| 본문 텍스트 추출 | 지원 | HWP `BodyText/Section*`의 `PARA_TEXT` 레코드를 해석하고, inline 제어문자 때문에 빠지는 연도별 수치 등을 보정합니다. |
| 제목/목록 추론 | 부분 지원 | `[1]`, `붙임1`, 짧은 `1. 제목`, `가. 제목` 등은 heading으로 승격합니다. |
| 표 | 부분 지원 | HWP/HWPX 표를 복원합니다. 단순 표는 GFM, 병합 셀이 있는 표는 HTML fallback으로 출력합니다. |
| 보도자료 metadata | 부분 지원 | 상단 보도자료 표의 배포일시, 보도일시, 담당부서, 담당자를 frontmatter와 report에 기록합니다. |
| 이미지/첨부 추출 | 지원 | HWP `BinData/*`, HWPX `BinData/*`를 assets 폴더로 추출합니다. HWP BMP 이미지는 Markdown preview 호환성을 위해 PNG로 변환하고, 공백/괄호가 있는 경로는 안전한 Markdown 링크로 출력합니다. 캡션 뒤 그림 control을 감지하면 해당 위치에 우선 배치합니다. |
| 각주/수식 | 부분 지원 | HWP 수식 script는 가능한 경우 Markdown 수식 fallback으로, 각주는 placeholder로 보존하고 report에 기록합니다. |
| 변환 리포트 | 지원 | 손실·경고·블록 수·자산 수를 JSON으로 생성합니다. |

## 샘플 검증 결과

이 폴더에 있던 샘플 `.hwp` 파일로 일괄 변환을 수행했으며, 모든 파일이 크래시 없이 Markdown과 리포트를 생성했습니다. 샘플 변환 결과는 `sample_output/` 폴더에 포함되어 있습니다.

## 테스트

현재 회귀 테스트는 표/metadata/inline 수치 복원/제목 추론/자산 링크/BMP 변환/수식 fallback/assets 정리를 중심으로 구성되어 있습니다.

```powershell
.\.venv\Scripts\python.exe -m unittest
```

## 라이선스 및 고지

본 프로젝트는 한글과컴퓨터와 제휴, 후원, 승인 관계가 없는 독립 도구입니다. “한글”, “한컴”, “HWP”, “HWPX” 등 상표는 각 권리자에게 귀속됩니다. 변환 결과는 원본 문서의 모든 레이아웃을 보장하지 않으므로 제출·배포 전 사용자가 반드시 검수해야 합니다.
