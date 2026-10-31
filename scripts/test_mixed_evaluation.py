"""Check separation metrics with known signals, independent from real inference."""
import numpy as np
import pytest
from scripts.evaluate_mixed import si_sdr,separation_metrics


def test_known_projection_leakage_and_sdr_order():
    t=np.arange(2000)/2000
    a=np.sin(2*np.pi*7*t)[:,None];b=np.cos(2*np.pi*11*t)[:,None]
    result=separation_metrics({"a":a,"b":b},{"a":a+.5*b,"b":b},a+b)
    assert result["a"]["projection_leakage_fraction"]==pytest.approx(.2)
    assert result["a"]["projection_target_to_interference_db"]==pytest.approx(10*np.log10(4))
    assert result["a"]["si_sdr_improvement_db"]>0
    assert result["b"]["projection_leakage_fraction"]==pytest.approx(0,abs=1e-12)
    assert si_sdr(a,a)>si_sdr(a,a+b)


def test_silent_outputs_have_no_invented_sdr():
    assert si_sdr(np.ones(100),np.zeros(100)) is None
