"""ESS optimizer core: dynamic programming over battery energy in 15-minute steps.

Plain Python without Home Assistant imports. It is shared by the Pyscript optimizer
(/config/pyscript/ess_optimizer.py loads it from /config/ess_lib) and the offline backtest.
Design: docs/optimizer.md.

All energy values are kWh per quarter (AC side unless noted), prices SEK/kWh, index 0 = current quarter.
"""

import math
from dataclasses import dataclass

DT = 0.25  # hours per step
EPS = 0.03  # kWh per quarter (~120 W) below which a flow counts as zero


@dataclass
class Params:
    capacity: float = 28.0  # kWh
    min_soc: float = 8.0  # %
    max_soc_grid: float = 95.0  # % ceiling for grid charging
    rte: float = 0.85  # round-trip efficiency AC -> AC
    wear: float = 0.05  # SEK per kWh discharged (DC)
    p_charge: float = 11.0  # kW AC into the battery
    p_discharge: float = 12.0  # kW AC out of the battery
    max_import: float = 10.0  # kW
    max_grid_charge: float = 9.0  # kW from the grid into the battery
    max_export: float = 10.0  # kW
    allow_grid_charge: bool = True
    allow_batt_export: bool = True
    allow_solar_export: bool = True
    block_negative: bool = True  # never export at a negative export price
    allow_batt_to_ev: bool = True
    reserve_margin: float = 0.2  # night reserve margin on the export floor
    reserve_pv_factor: float = 0.7  # pessimistic PV factor for the export floor
    de: float = 0.2  # kWh state grid step
    full_charge_index: int = -1  # quarter index by which the battery must be full (-1 = none)
    shortfall_penalty: float = 5.0  # SEK/kWh missing at full_charge_index
    terminal_percentile: float = 0.10  # import-price percentile that values energy left for the night
    absorb_surplus: bool = False  # PV surplus must charge the battery when it can (no PV export by choice)
    sell_mask: list = None  # per-quarter selling permission (None = all quarters); combined with allow_batt_export
    spike_mask: list = None  # quarters opened only by the spike exception: their reserve counts cheap grid quarters
    # Efficiency model. "constant": sqrt(rte) each way. "measured": the loss curves below, fitted 2026-10-17 on
    # 5-minute data of this system (nights, no PV); they include the inverter's own consumption when active.
    eff_model: str = "constant"
    dis_a: float = 0.9953  # inverter (battery -> AC): AC kW = dis_a * DC kW - dis_b
    dis_b: float = 0.0725
    chg_a: float = 0.9452  # charger (AC -> battery): DC kW = chg_a * AC kW - chg_q * AC kW^2 - chg_b
    chg_q: float = 0.0060
    chg_b: float = 0.0581
    batt_eff: float = 0.96  # battery round trip at its terminals (SmartShunt counters, 12 months)
    standby_kw: float = 0.045  # AC draw of the inverter while the battery is idle
    ev_start_cost: float = 0.0  # SEK per EV charge start: > 0 groups the EV quarters into blocks (schedule_ev)


def ac_for(p, d_e):
    """AC energy (kWh per quarter) into the battery path for a change d_e of the stored energy; negative = delivered."""
    if p.eff_model != "measured":
        eta = math.sqrt(p.rte)
        return d_e / eta if d_e >= 0 else d_e * eta
    eb = math.sqrt(p.batt_eff)
    if abs(d_e) < 1e-9:
        return p.standby_kw * DT
    if d_e > 0:
        dc = d_e / eb / DT
        if p.chg_q > 0:
            ac = (p.chg_a - math.sqrt(max(p.chg_a ** 2 - 4 * p.chg_q * (dc + p.chg_b), 0.0))) / (2 * p.chg_q)
        else:
            ac = (dc + p.chg_b) / p.chg_a
        return ac * DT
    dc = -d_e * eb / DT
    return -(p.dis_a * dc - p.dis_b) * DT


def de_for_ac(p, ac):
    """Inverse of ac_for: stored-energy change for AC energy into (ac > 0) or out of (ac < 0) the battery path."""
    if p.eff_model != "measured":
        eta = math.sqrt(p.rte)
        return ac * eta if ac >= 0 else ac / eta
    eb = math.sqrt(p.batt_eff)
    if ac > 0:
        a = ac / DT
        return max(p.chg_a * a - p.chg_q * a * a - p.chg_b, 0.0) * DT * eb
    if ac < 0:
        return -((-ac / DT + p.dis_b) / p.dis_a) * DT / eb
    return 0.0


def eta_pair(p):
    """Typical one-way efficiencies (charge, discharge) for scalar estimates; measured: charge at 5 kW, deliver 1 kW."""
    if p.eff_model == "measured":
        return de_for_ac(p, 5 * DT) / (5 * DT), (1 * DT) / -de_for_ac(p, -1 * DT)
    eta = math.sqrt(p.rte)
    return eta, eta


def stored_need(p, deficit):
    """Stored energy that covers an AC deficit of one quarter (the constant model keeps the AC value, as before)."""
    if deficit <= 0:
        return 0.0
    return -de_for_ac(p, -deficit) if p.eff_model == "measured" else deficit


def export_floor(p, load, ev, pv):
    """Battery energy (kWh) that must remain before selling at each quarter.

    min SoC + pessimistic energy deficit until the next quarter with a PV surplus, times (1 + margin).
    Arrays may extend beyond the price horizon (e.g. the day after tomorrow) for a better night estimate.
    """
    e_min = p.capacity * p.min_soc / 100
    n = len(load)
    need = [0.0] * (n + 1)
    for k in range(n - 1, -1, -1):
        house = load[k] + (ev[k] if p.allow_batt_to_ev else 0.0)
        surplus = pv[k] * p.reserve_pv_factor - house
        need[k] = 0.0 if surplus > EPS else stored_need(p, max(house - pv[k] * p.reserve_pv_factor, 0.0)) + need[k + 1]
    return [min(p.capacity, e_min + need[k] * (1 + p.reserve_margin)) for k in range(n)]


def export_floor_grid(p, load, ev, pv, pimp, cheap_fraction=0.25):
    """Export floor for spike sales: like export_floor, but a cheap grid quarter (the cheapest cheap_fraction of
    the known import prices) also counts as a refill point, since the battery can be recharged there.
    Beyond the price horizon only PV surplus refills (conservative)."""
    e_min = p.capacity * p.min_soc / 100
    known = sorted(pimp)
    cheap = known[int(len(known) * cheap_fraction)] if known else -1e9
    n = len(load)
    need = [0.0] * (n + 1)
    for k in range(n - 1, -1, -1):
        house = load[k] + (ev[k] if p.allow_batt_to_ev else 0.0)
        surplus = pv[k] * p.reserve_pv_factor - house
        grid_refill = p.allow_grid_charge and k < len(pimp) and pimp[k] <= cheap
        need[k] = 0.0 if (surplus > EPS or grid_refill) else stored_need(p, max(house - pv[k] * p.reserve_pv_factor, 0.0)) + need[k + 1]
    return [min(p.capacity, e_min + need[k] * (1 + p.reserve_margin)) for k in range(n)]


def sell_floor(p, load, ev, pv, pimp, mask_normal, mask_spike):
    """Per-quarter export floor: the PV-refill floor where normal selling rules allow selling, the grid-refill
    floor where only the spike exception does."""
    f_pv = export_floor(p, load, ev, pv)
    if not any(mask_spike):
        return f_pv
    f_grid = export_floor_grid(p, load, ev, pv, pimp)
    return [f_grid[k] if (k < len(mask_spike) and mask_spike[k] and not mask_normal[k]) else f_pv[k]
            for k in range(len(f_pv))]


def _step(p, eta_c, eta_d, load, ev, pv, pimp, pexp, floor, e_from, e_to, sell_ok=True):
    """Cost of moving the battery from e_from to e_to in one quarter; None if not allowed.

    Returns (cost, b_ac, g, grid_charge, batt_export, exported, curtailed).
    """
    d_e = e_to - e_from
    b_ac, dis_dc = ac_for(p, d_e), max(-d_e, 0.0)
    if b_ac > p.p_charge * DT + 1e-9 or -b_ac > p.p_discharge * DT + 1e-9:
        return None
    net = load + ev - pv
    g = net + b_ac
    surplus = max(-net, 0.0)
    if (p.absorb_surplus or not sell_ok) and surplus > 1e-9:
        # Take at least what plain self-consumption would take in (a surplus too small to charge through the
        # measured charger's offset is not absorbable; requiring it made such states infeasible).
        absorb = ac_for(p, _self_next(p, e_from, -surplus, p.capacity * p.min_soc / 100) - e_from)
        if b_ac < absorb - 1e-6:
            return None
    grid_charge = b_ac - surplus if (d_e > 1e-12 and b_ac > surplus) else 0.0
    if grid_charge > 1e-9:
        if (not p.allow_grid_charge or e_to > p.capacity * p.max_soc_grid / 100 + 1e-9
                or grid_charge > p.max_grid_charge * DT + 1e-9):
            return None
    deficit = max(net if p.allow_batt_to_ev else load - pv, 0.0)
    batt_export = -b_ac - deficit if (d_e < -1e-12 and -b_ac > deficit) else 0.0
    if batt_export > 1e-9 and (not p.allow_batt_export or not sell_ok or e_to < floor - 1e-9):
        return None
    if g > p.max_import * DT + 1e-9:
        return None
    exported = curtailed = 0.0
    if g >= 0:
        cost = g * pimp
    else:
        exported = -g
        if not p.allow_solar_export or (p.block_negative and pexp < 0):
            exported = min(exported, batt_export) if pexp >= 0 or not p.block_negative else 0.0
        exported = min(exported, p.max_export * DT)
        curtailed = -g - exported
        cost = -exported * pexp
    return cost + p.wear * dis_dc, b_ac, g, grid_charge, batt_export, exported, curtailed


def optimize(p, soc, load, ev, pv, pimp, pexp, floor=None):
    """Optimal battery schedule over the price horizon (len(pimp) quarters).

    load/ev/pv may be longer than the horizon (used for the export floor and the terminal value).
    The value function lives on an energy grid (step p.de) and is interpolated between grid points; each
    step also offers the exact self-consumption move, so small loads are followed without quantisation.
    """
    T = len(pimp)
    if floor is None:
        floor = export_floor(p, load, ev, pv)
        if p.spike_mask and any(p.spike_mask):
            f_grid = export_floor_grid(p, load, ev, pv, pimp)
            floor = [f_grid[k] if (k < len(p.spike_mask) and p.spike_mask[k]) else floor[k] for k in range(len(floor))]
    eta_c, eta_d = eta_pair(p)
    e_min = p.capacity * p.min_soc / 100
    n = int(round((p.capacity - e_min) / p.de)) + 1
    levels = [e_min + i * p.de for i in range(n)]
    max_up = int(p.p_charge * DT * eta_c / p.de) + 1
    max_down = int(p.p_discharge * DT / eta_d / p.de) + 1

    # Terminal value of energy left at the horizon end. Energy that tomorrow's PV surplus would push out of a
    # full battery (spilled to the grid) is only worth the median export price; everything below that level
    # replaces later imports and is worth a low import-price percentile. The night need until the next PV
    # surplus is always valued at the import price. In autumn/winter the surplus cannot fill the battery,
    # so all stored energy keeps its import value (selling it cheaply would have to be bought back).
    imp_sorted, exp_sorted = sorted(pimp), sorted(pexp)
    value_end = imp_sorted[int(len(imp_sorted) * p.terminal_percentile)] if imp_sorted else 0.0
    value_low = max(exp_sorted[len(exp_sorted) // 2], 0.0) if exp_sorted else 0.0
    if len(load) > T and len(floor) > T:
        need_after = (floor[T] - e_min) / (1 + p.reserve_margin)
    else:
        need_after = 8 * 4 * (sum(load[:T]) / max(T, 1))  # no forecast beyond: assume 8 h of average load
    beyond = range(T, min(len(load), len(pv), T + 96))
    surplus_next = sum([max(pv[k] - load[k] - ev[k], 0.0) for k in beyond])  # 0 when no forecast beyond
    usable_max = (p.capacity - e_min) * eta_d
    keep = min(usable_max, max(need_after, usable_max - surplus_next * eta_c * eta_d))

    def terminal(e):
        usable = (e - e_min) * eta_d
        high = min(usable, keep)
        return -(high * value_end + (usable - high) * value_low)

    def interp(V, e):
        x = (e - e_min) / p.de
        if x <= 0:
            return V[0]
        if x >= n - 1:
            return V[n - 1]
        k = int(x)
        f = x - k
        return V[k] * (1 - f) + V[k + 1] * f

    def self_target(t, e):
        """Battery energy after exactly covering / absorbing the net load (plain self-consumption)."""
        net = load[t] + ev[t] - pv[t]
        if not p.allow_batt_to_ev and load[t] - pv[t] > 0:
            net = load[t] - pv[t]
        return _self_next(p, e, net, e_min)

    def best_move(t, e, Vn):
        ok = p.sell_mask is None or (t < len(p.sell_mask) and p.sell_mask[t])
        lo = max(0, int(math.floor((e - e_min) / p.de)) - max_down)
        hi = min(n, int(math.ceil((e - e_min) / p.de)) + max_up + 1)
        best, best_e, best_r = math.inf, e, None
        l, v, s, pi, pe, fl = load[t], ev[t], pv[t], pimp[t], pexp[t], floor[t]
        for e2 in [levels[j] for j in range(lo, hi)] + [self_target(t, e), e]:
            r = _step(p, eta_c, eta_d, l, v, s, pi, pe, fl, e, e2, ok)
            if r is None:
                continue
            total = r[0] + interp(Vn, e2)
            if t == p.full_charge_index:
                total += p.shortfall_penalty * (p.capacity - e2)
            if total < best - 1e-9:
                best, best_e, best_r = total, e2, r
        return best, best_e, best_r

    Vs = [None] * (T + 1)
    Vs[T] = [terminal(e) for e in levels]
    for t in range(T - 1, -1, -1):
        Vn = Vs[t + 1]
        Vs[t] = [best_move(t, e, Vn)[0] for e in levels]

    # Forward pass with the continuous battery energy, re-deciding each step against the stored values.
    e = min(max(soc / 100 * p.capacity, e_min), p.capacity)
    out = {k: [] for k in ("mode", "soc", "batt", "grid", "grid_charge", "batt_export", "exported",
                           "curtailed", "floor", "lam", "cost")}
    for t in range(T):
        _, e2, r = best_move(t, e, Vs[t + 1])
        if r is None:  # nothing allowed: fall back to plain self-consumption (the Victron default)
            e2 = self_target(t, e)
            r = _step(p, eta_c, eta_d, load[t], ev[t], pv[t], pimp[t], pexp[t], -1.0, e, e2) or (0, 0, 0, 0, 0, 0, 0)
        cost, b_ac, g, gc, bx, exported, curtailed = r
        out["mode"].append(classify(load[t] + ev[t] - pv[t], b_ac, gc, bx, e2, p.capacity, p.de))
        out["soc"].append(e2 / p.capacity * 100)
        out["batt"].append(b_ac)
        out["grid"].append(g)
        out["grid_charge"].append(gc)
        out["batt_export"].append(bx)
        out["exported"].append(exported)
        out["curtailed"].append(curtailed)
        out["floor"].append(floor[t] / p.capacity * 100)
        out["cost"].append(cost)
        e = e2
    out["lam"] = marginal_costs(p, soc, load, ev, pv, pimp, pexp, floor, out)
    out["total_cost"] = sum(out["cost"])
    out["value_end"] = value_end
    # Cost including the value of the energy left at the horizon end, comparable with the baseline.
    out["net_cost"] = out["total_cost"] + terminal(e)
    out["terminal"] = terminal
    return out


def marginal_costs(p, soc, load, ev, pv, pimp, pexp, floor, plan):
    """Approximate cost of one extra kWh of load in each quarter, given the plan.

    Import price when the grid supplies the margin, export price when PV would otherwise be exported,
    or the value of stored energy (import price of the cheapest later quarter that the battery would
    otherwise have served, after round-trip losses) when the battery covers it.
    """
    T = len(pimp)
    eta_c, eta_d = eta_pair(p)
    lam = []
    for t in range(T):
        if plan["grid"][t] > EPS or plan["grid_charge"][t] > EPS:
            lam.append(pimp[t])
        elif plan["exported"][t] > EPS:
            lam.append(pexp[t])
        elif plan["curtailed"][t] > EPS:
            lam.append(0.0)
        else:
            # Battery covers the margin: the energy is missing later, where it replaces an import.
            later = [pimp[k] for k in range(t + 1, T) if plan["grid"][k] > EPS]
            lam.append(min(later) / (eta_c * eta_d) if later else pexp[t] if pexp[t] > 0 else 0.0)
    return lam


def classify(net, b_ac, grid_charge, batt_export, e_to, capacity, de):
    deficit, surplus = max(net, 0.0), max(-net, 0.0)
    if grid_charge > EPS:
        return "CHARGE"
    if batt_export > EPS:
        return "EXPORT"
    if deficit > EPS and -b_ac < 0.5 * deficit:
        return "HOLD"
    if surplus > EPS and b_ac < 0.5 * surplus and e_to < capacity - de:
        return "SOLAR_EXPORT"
    return "SELF"


def _self_next(p, e, net, e_min):
    """Battery energy after exactly covering (net > 0) or absorbing (net < 0) the net load: plain self-consumption."""
    if net >= 0:
        return max(e + de_for_ac(p, -min(net, p.p_discharge * DT)), e_min) if e > e_min else e
    return min(e + de_for_ac(p, min(-net, p.p_charge * DT)), p.capacity) if e < p.capacity else e


def execute_quarter(p, mode, plan_g, floor_e, e, load, pv, pimp, pexp):
    """Simulate one quarter of the executor against actual load/PV. Returns (new stored energy, cost)."""
    e_min = p.capacity * p.min_soc / 100
    net = load - pv
    lo = e_min
    if mode in ("CHARGE", "EXPORT"):
        target = plan_g - net  # the executor holds the planned grid power; the battery takes the rest
        if mode == "EXPORT":
            lo = max(e_min, min(floor_e, e - 0.01 * p.capacity))
    else:
        target = -net
        if mode == "HOLD" and target < 0:
            target = 0.0
        if mode == "SOLAR_EXPORT" and target > 0:
            target = 0.0
    target = max(-p.p_discharge * DT, min(p.p_charge * DT, target))
    d = de_for_ac(p, target) if abs(target) > 1e-12 else 0.0
    if d < 0:
        e2 = max(e + d, lo) if e > lo else e
    else:
        e2 = min(e + d, p.capacity)
    g = net + ac_for(p, e2 - e)
    cost = g * pimp if g >= 0 else (g * pexp if not (p.block_negative and pexp < 0) else 0.0)
    return e2, cost + p.wear * max(e - e2, 0.0)


def self_consumption(p, soc, load, ev, pv, pimp, pexp):
    """Baseline: plain Victron self-consumption (no grid charging, no battery export)."""
    e_min = p.capacity * p.min_soc / 100
    e = min(max(soc / 100 * p.capacity, e_min), p.capacity)
    total = 0.0
    socs = []
    for t in range(len(pimp)):
        net = load[t] + ev[t] - pv[t]
        e2 = _self_next(p, e, net, e_min)
        g = net + ac_for(p, e2 - e)
        total += p.wear * max(e - e2, 0.0)
        e = e2
        if g >= 0:
            total += g * pimp[t]
        else:
            exported = 0.0 if (not p.allow_solar_export or (p.block_negative and pexp[t] < 0)) else min(-g, p.max_export * DT)
            total -= exported * pexp[t]
        socs.append(e / p.capacity * 100)
    return total, socs


def _ev_blocks(lam, window, count, per_quarter_kwh, start_cost, charging_now):
    """Choose `count` quarters of `window` (consecutive indices) at the lowest energy cost plus start_cost per
    charge start (a chosen quarter after an unchosen one). The first quarter starts for free when the car is
    already charging, so a running charge is not interrupted for a few öre. Ties prefer later quarters.
    Returns the chosen indices, ascending."""
    n = len(window)
    inf = math.inf
    # cost[k][s]: best cost with k quarters chosen so far, s = 1 if the previous quarter was chosen.
    cost = [[inf, inf] for _ in range(count + 1)]
    cost[0][1 if charging_now else 0] = 0.0
    back = []
    for i in range(n):
        t = window[i]
        price = lam[t] * per_quarter_kwh - 1e-7 * i
        new = [[inf, inf] for _ in range(count + 1)]
        ptr = [[None, None] for _ in range(count + 1)]
        for k in range(min(i, count) + 1):
            for s in (0, 1):
                c = cost[k][s]
                if c == inf:
                    continue
                if c < new[k][0]:  # skip this quarter
                    new[k][0], ptr[k][0] = c, (k, s)
                if k < count:  # charge in this quarter
                    c2 = c + price + (0.0 if s == 1 else start_cost)
                    if c2 < new[k + 1][1]:
                        new[k + 1][1], ptr[k + 1][1] = c2, (k, s)
        cost = new
        back.append(ptr)
    s = 0 if cost[count][0] <= cost[count][1] else 1
    k = count
    chosen = []
    for i in range(n - 1, -1, -1):
        prev = back[i][k][s]
        if prev is None:
            break
        if s == 1:
            chosen.append(window[i])
        k, s = prev
    return sorted(chosen)


def schedule_ev(lam, need_kwh, per_quarter_kwh, deadline, home_from=0, opp_kwh=0.0, opp_max_price=None,
                start_cost=0.0, charging_now=False):
    """Pick EV charging quarters: the cheapest by lambda before the deadline, latest first on ties. With
    start_cost > 0 each charge start costs that much (SEK), so quarters whose prices differ by a few öre are
    grouped into blocks instead of many short charges (2026-11-12: 3-7 starts per night).

    Returns (kWh per quarter list, feasible flag).
    """
    T = len(lam)
    ev = [0.0] * T
    feasible = True
    if need_kwh > 0 and per_quarter_kwh > 0:
        end = min(deadline, T)
        window = list(range(home_from, end))
        count = math.ceil(need_kwh / per_quarter_kwh - 1e-9)
        if count > len(window):
            feasible = False
            count = len(window)
        if start_cost > 0 and 0 < count < len(window):
            picked = _ev_blocks(lam, window, count, per_quarter_kwh, start_cost, charging_now and home_from == 0)
        else:
            picked = sorted(window, key=lambda k: (lam[k], -k))[:count]
        for t in picked:
            ev[t] = per_quarter_kwh
        # The last chosen quarter only needs the remainder.
        chosen = sorted([t for t in window if ev[t] > 0])
        rest = need_kwh - per_quarter_kwh * (len(chosen) - 1)
        if chosen and 0 < rest < per_quarter_kwh:
            ev[chosen[-1]] = rest
    if opp_kwh > 0 and opp_max_price is not None:
        cands = [t for t in range(home_from, T) if ev[t] == 0 and lam[t] <= opp_max_price]
        for t in sorted(cands, key=lambda k: (lam[k], -k)):
            if opp_kwh <= 0:
                break
            ev[t] = min(per_quarter_kwh, opp_kwh)
            opp_kwh -= ev[t]
    return ev, feasible


def plan_with_ev(p, soc, load, pv, pimp, pexp, ev_need=0.0, ev_q=0.9, ev_deadline=0, ev_home_from=0,
                 opp_kwh=0.0, opp_max_price=None, ev_fixed=None, ev_charging_now=False):
    """House battery plan coupled with the EV schedule (two rounds via marginal costs)."""
    T = len(pimp)
    n_ext = len(load)
    zeros = [0.0] * n_ext
    ev_ext = list(ev_fixed) if ev_fixed is not None else zeros
    plan = optimize(p, soc, load, ev_ext, pv, pimp, pexp)
    feasible = True
    if ev_fixed is None and (ev_need > 0 or opp_kwh > 0):
        for _ in range(2):
            ev, feasible = schedule_ev(plan["lam"], ev_need, ev_q, ev_deadline, ev_home_from, opp_kwh, opp_max_price,
                                       p.ev_start_cost, ev_charging_now)
            ev_ext = ev + [0.0] * (n_ext - T)
            plan = optimize(p, soc, load, ev_ext, pv, pimp, pexp)
    plan["ev"] = ev_ext[:T]
    plan["ev_feasible"] = feasible
    base_cost, base_soc = self_consumption(p, soc, load, ev_ext, pv, pimp, pexp)
    e_min = p.capacity * p.min_soc / 100
    e_end = base_soc[-1] / 100 * p.capacity if base_soc else e_min
    plan["baseline_cost"] = base_cost
    plan["baseline_net_cost"] = base_cost + plan["terminal"](e_end)
    plan["baseline_soc"] = base_soc
    plan["savings"] = plan["baseline_net_cost"] - plan["net_cost"]
    return plan
