# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Yong Park
"""공용 pytest 설정.

현재는 프로젝트 전역에서 공유하는 픽스처가 없다. 각 테스트 모듈은
자신에게 필요한 픽스처를 해당 테스트 파일 안에서 직접 정의한다.
이 파일은 tests 패키지의 루트 conftest 자리를 확보하기 위한 스캐폴드다.
"""
