"""StateEngine: 狀態判定。

全部是純函數測試 —— 不碰檔案系統、不碰 LSF。這正是把判定邏輯做成純函數的
價值: 可以在毫秒內窮舉「job 跑了三天才卡住」這種真實情況下要等三天的情境。
"""

import unittest

from arcx_auto.domain.enums import CaseState, Completeness, LsfState, MarkerKind
from arcx_auto.domain.models import CaseObservation, LsfJobView
from arcx_auto.services.state_engine import (
    TransitionContext,
    classify_completeness,
    transition_case,
)

T0 = 1_000_000.0


def obs(case_id="case1", markers=(), log_size=None, log_mtime=None,
        case_dir=None, lsf=None, log_path=None):
    return CaseObservation(
        case_id=case_id,
        markers=frozenset(markers),
        case_dir=case_dir,
        case_dir_exists=case_dir is not None,
        log_path=log_path or ("/tmp/l.log" if log_size is not None else None),
        log_size=log_size,
        log_mtime=log_mtime,
        lsf=lsf,
    )


def ctx(now=T0, stall=3600.0, grace=300.0, lsf_available=True):
    return TransitionContext(
        now=now, stall_threshold_sec=stall, lost_grace_sec=grace,
        lsf_data_available=lsf_available,
    )


class BasicTransitionTest(unittest.TestCase):
    def test_no_marker_is_pending(self):
        snap, _ = transition_case(None, obs(), ctx())
        self.assertEqual(snap.state, CaseState.PENDING)

    def test_queue_marker(self):
        snap, _ = transition_case(None, obs(markers=[MarkerKind.QUEUE]), ctx())
        self.assertEqual(snap.state, CaseState.QUEUED)

    def test_run_marker(self):
        snap, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100), ctx())
        self.assertEqual(snap.state, CaseState.RUNNING)

    def test_complete_marker(self):
        snap, _ = transition_case(None, obs(markers=[MarkerKind.COMPLETE]), ctx())
        self.assertEqual(snap.state, CaseState.COMPLETED_MARKER)

    def test_complete_wins_over_run(self):
        """marker 優先序 complete > run > queue。

        .run 沒被清掉不影響「Arcx 認為跑完了」這個事實, 但要記錄不一致。
        """
        snap, _ = transition_case(
            None, obs(markers=[MarkerKind.COMPLETE, MarkerKind.RUN]), ctx())
        self.assertEqual(snap.state, CaseState.COMPLETED_MARKER)
        self.assertTrue(snap.marker_inconsistent)

    def test_complete_marker_is_not_done(self):
        """.complete 只代表 Arcx 認為跑完, 不代表結果正確。

        DONE 必須等 QA 驗證通過 (Phase 1) —— 這個分離是抓「假成功」的關鍵。
        """
        snap, _ = transition_case(None, obs(markers=[MarkerKind.COMPLETE]), ctx())
        self.assertNotEqual(snap.state, CaseState.DONE)

    def test_orphan_dir_is_pending_with_reason(self):
        snap, _ = transition_case(None, obs(case_dir="/x/case9"), ctx())
        self.assertEqual(snap.state, CaseState.PENDING)
        self.assertIn("marker", snap.note or "")


class ProgressTest(unittest.TestCase):
    def test_log_growth_resets_silence(self):
        c = ctx(now=T0)
        first, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0), c)
        later = ctx(now=T0 + 1800)
        second, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=200), later)
        self.assertEqual(second.last_progress_at, T0 + 1800)
        self.assertEqual(second.silent_for(T0 + 1800), 0.0)

    def test_log_not_growing_accumulates_silence(self):
        c = ctx(now=T0)
        first, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0), c)
        later = ctx(now=T0 + 1800)
        second, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=100), later)
        self.assertEqual(second.silent_for(T0 + 1800), 1800.0)

    def test_stall_detected_past_threshold(self):
        """STALLED 的定義是「job 還活著但沒有進度」, 所以必須有活著的 LSF job。"""
        alive = LsfJobView(job_id="7", state=LsfState.RUN)
        c = ctx(now=T0)
        first, _ = transition_case(
            None,
            obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0, lsf=alive),
            c,
        )
        later = ctx(now=T0 + 3700, stall=3600.0)
        second, _ = transition_case(
            first,
            obs(markers=[MarkerKind.RUN], log_size=100, lsf=alive),
            later,
        )
        self.assertEqual(second.state, CaseState.STALLED)

    def test_lost_takes_priority_over_stalled(self):
        """job 消失 + log 不動 -> LOST 而非 STALLED。

        兩者都成立時 LOST 是更精確的診斷 (job 死了), 而 STALLED 會誤導工程師
        去查「為什麼跑很慢」, 實際上它根本沒在跑。
        """
        c = ctx(now=T0, grace=300.0, stall=3600.0)
        first, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0), c)
        second, _ = transition_case(
            first,
            obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 3700, grace=300.0, stall=3600.0),
        )
        self.assertEqual(second.state, CaseState.LOST)

    def test_first_observation_seeds_silence_from_mtime(self):
        """關鍵: 首次觀測 (或 daemon 重啟後) 必須用 mtime 當起算點。

        若一律從 now 起算, 一個卡住三天的 case 每次重啟都會看起來很健康,
        而系統設計要求「重啟後能從檔案系統重建全部狀態」。
        """
        snap, _ = transition_case(
            None,
            obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0 - 7200),
            ctx(now=T0, stall=3600.0),
        )
        self.assertEqual(snap.state, CaseState.STALLED)

    def test_future_mtime_is_clamped(self):
        """時鐘不同步時不該出現負的靜止時間。"""
        snap, _ = transition_case(
            None,
            obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0 + 5000),
            ctx(now=T0),
        )
        self.assertEqual(snap.last_progress_at, T0)
        self.assertEqual(snap.silent_for(T0), 0.0)


class LsfTest(unittest.TestCase):
    def test_suspended_from_lsf(self):
        job = LsfJobView(job_id="1", state=LsfState.SSUSP)
        snap, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=1, lsf=job), ctx())
        self.assertEqual(snap.state, CaseState.SUSPENDED)

    def test_lost_requires_grace_period(self):
        """LSF job 消失後不能立刻判 LOST —— marker 與 LSF 的可見性有落差。"""
        c = ctx(now=T0, grace=300.0)
        first, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0), c)
        # job 消失, 但還在 grace 期內
        mid, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 100, grace=300.0))
        self.assertEqual(mid.state, CaseState.RUNNING)
        # 超過 grace
        late, _ = transition_case(
            mid, obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 400, grace=300.0))
        self.assertEqual(late.state, CaseState.LOST)

    def test_never_lost_when_lsf_data_unavailable(self):
        """bjobs 抽風時絕不能把所有 case 判成 LOST。

        寧可停在 RUNNING 讓人看到, 也不要誤報一整片。
        """
        c = ctx(now=T0, lsf_available=False)
        first, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0), c)
        late, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 100000, lsf_available=False, stall=1e9))
        self.assertEqual(late.state, CaseState.RUNNING)

    def test_lost_clears_when_job_reappears(self):
        c = ctx(now=T0, grace=300.0)
        first, _ = transition_case(
            None, obs(markers=[MarkerKind.RUN], log_size=100, log_mtime=T0), c)
        gone, _ = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=100),
            ctx(now=T0 + 100, grace=300.0))
        self.assertIsNotNone(gone.lsf_missing_since)
        job = LsfJobView(job_id="7", state=LsfState.RUN)
        back, _ = transition_case(
            gone, obs(markers=[MarkerKind.RUN], log_size=100, lsf=job),
            ctx(now=T0 + 200, grace=300.0))
        self.assertIsNone(back.lsf_missing_since)
        self.assertEqual(back.state, CaseState.RUNNING)

    def test_completed_case_never_judged_lost(self):
        """已完成的 case 沒有 LSF job 是正常的, 不該被判 LOST。"""
        snap, _ = transition_case(
            None, obs(markers=[MarkerKind.COMPLETE]), ctx(now=T0))
        self.assertEqual(snap.state, CaseState.COMPLETED_MARKER)


    def test_queued_case_also_goes_lost_when_job_never_appears(self):
        """.queue marker 存在但 LSF 從頭到尾沒有這個 job -> 從未真的被提交。

        grace period 涵蓋 Arcx「先寫 marker 再 bsub」的時間差。
        """
        c = ctx(now=T0, grace=300.0)
        first, _ = transition_case(None, obs(markers=[MarkerKind.QUEUE]), c)
        self.assertEqual(first.state, CaseState.QUEUED)
        later, _ = transition_case(
            first, obs(markers=[MarkerKind.QUEUE]),
            ctx(now=T0 + 400, grace=300.0))
        self.assertEqual(later.state, CaseState.LOST)


class EventTest(unittest.TestCase):
    def test_event_emitted_on_first_observation(self):
        _snap, events = transition_case(None, obs(markers=[MarkerKind.QUEUE]), ctx())
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].from_state)
        self.assertEqual(events[0].to_state, CaseState.QUEUED)

    def test_no_event_when_state_unchanged(self):
        c = ctx(now=T0, lsf_available=False)
        first, _ = transition_case(None, obs(markers=[MarkerKind.QUEUE]), c)
        _second, events = transition_case(
            first, obs(markers=[MarkerKind.QUEUE]),
            ctx(now=T0 + 60, lsf_available=False))
        self.assertEqual(events, [])

    def test_event_on_transition_carries_evidence(self):
        c = ctx(now=T0)
        first, _ = transition_case(None, obs(markers=[MarkerKind.QUEUE]), c)
        _second, events = transition_case(
            first, obs(markers=[MarkerKind.RUN], log_size=10), ctx(now=T0 + 60))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].from_state, CaseState.QUEUED)
        self.assertEqual(events[0].to_state, CaseState.RUNNING)
        self.assertIn("markers", events[0].evidence)

    def test_entered_state_at_preserved_while_state_unchanged(self):
        c = ctx(now=T0, lsf_available=False)
        first, _ = transition_case(None, obs(markers=[MarkerKind.QUEUE]), c)
        second, _ = transition_case(
            first, obs(markers=[MarkerKind.QUEUE]),
            ctx(now=T0 + 999, lsf_available=False))
        self.assertEqual(second.entered_state_at, T0)


class CompletenessTest(unittest.TestCase):
    """rerun 刪除清單的判定 (architecture §6.1)。"""

    def _snap(self, state, inconsistent=False):
        from arcx_auto.domain.models import CaseSnapshot
        return CaseSnapshot(
            case_id="case1", state=state, entered_state_at=T0,
            last_progress_at=T0, marker_inconsistent=inconsistent,
        )

    def test_completed_marker_is_kept(self):
        result, _ = classify_completeness(self._snap(CaseState.COMPLETED_MARKER))
        self.assertEqual(result, Completeness.COMPLETE)

    def test_inconsistent_complete_is_unknown(self):
        result, _ = classify_completeness(
            self._snap(CaseState.COMPLETED_MARKER, inconsistent=True))
        self.assertEqual(result, Completeness.UNKNOWN)

    def test_in_flight_states_are_incomplete(self):
        for state in (CaseState.PENDING, CaseState.QUEUED, CaseState.RUNNING,
                      CaseState.SUSPENDED, CaseState.STALLED, CaseState.LOST):
            result, _ = classify_completeness(self._snap(state))
            self.assertEqual(result, Completeness.INCOMPLETE, state)

    def test_unknown_state_is_unknown_not_complete(self):
        """關鍵安全性質: 判不出來時絕不能傾向「保留」。

        漏刪未完成 -> 殘缺結果被當成功交付 (不可回收);
        誤刪已完成 -> 只是浪費一次運算  (可回收)。
        """
        result, _ = classify_completeness(self._snap(CaseState.UNKNOWN))
        self.assertEqual(result, Completeness.UNKNOWN)
        self.assertNotEqual(result, Completeness.COMPLETE)


if __name__ == "__main__":
    unittest.main()
