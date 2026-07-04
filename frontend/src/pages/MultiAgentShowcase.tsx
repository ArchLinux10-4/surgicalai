/**
 * MultiAgentShowcase — landing page section for the true multi-agent orchestration feature.
 * Self-contained: own CSS (sai-ma- prefix), own IntersectionObserver animation.
 * Wired into LandingPage.tsx between the Tasks and Pricing sections.
 */
import { useEffect, useRef } from 'react';

const CSS = `
/* MULTI-AGENT ORCHESTRATION */
.sai-ma-section{padding:110px 24px;background:#0d0b18;position:relative;overflow:hidden}
.sai-ma-section::before{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:radial-gradient(ellipse 55% 50% at 25% 20%,rgba(124,106,247,.14) 0%,transparent 65%);pointer-events:none}
.sai-ma-section::after{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:radial-gradient(ellipse 45% 45% at 80% 85%,rgba(15,168,118,.1) 0%,transparent 60%);pointer-events:none}
.sai-ma-wrap{max-width:1100px;margin:0 auto;position:relative;z-index:1}
.sai-ma-head{text-align:center;margin-bottom:64px}
.sai-ma-pill{display:inline-flex;align-items:center;gap:7px;background:rgba(124,106,247,.14);border:1px solid rgba(124,106,247,.4);color:#a78bfa;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:6px 16px;border-radius:100px;margin-bottom:24px}
.sai-ma-pill-dot{width:6px;height:6px;border-radius:50%;background:#a78bfa;animation:sai-pulse 2s ease-in-out infinite}
.sai-ma-h2{font-size:clamp(36px,5vw,58px);font-weight:800;letter-spacing:-1.5px;line-height:1.08;color:#fff;margin:0 0 20px}
.sai-ma-h2 .ma-grad{background:linear-gradient(135deg,#a78bfa 0%,#34d399 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sai-ma-sub{font-size:18px;color:rgba(255,255,255,.55);max-width:640px;margin:0 auto;line-height:1.65}
.sai-ma-console{background:#12101f;border:1px solid rgba(124,106,247,.25);border-radius:16px;overflow:hidden;box-shadow:0 24px 80px rgba(124,106,247,.18),0 4px 16px rgba(0,0,0,.35),0 0 0 1px rgba(124,106,247,.08)}
.sai-ma-bar{height:40px;background:#0c0a16;border-bottom:1px solid rgba(255,255,255,.06);display:flex;align-items:center;padding:0 14px;gap:8px}
.sai-ma-bar-title{margin-left:10px;font-size:12px;color:rgba(255,255,255,.35);font-weight:500;font-family:'JetBrains Mono',monospace}
.sai-ma-body{padding:20px 22px 22px;font-family:'JetBrains Mono','Fira Code',monospace}
.sai-ma-supervisor{display:flex;align-items:center;gap:10px;min-height:24px;font-size:12.5px;color:#a78bfa;font-weight:600}
.sai-ma-role-badge{font-size:10px;font-weight:800;letter-spacing:.6px;text-transform:uppercase;padding:3px 9px;border-radius:5px;flex-shrink:0}
.sai-ma-role-badge.sup{background:rgba(124,106,247,.2);color:#a78bfa;border:1px solid rgba(124,106,247,.35)}
.sai-ma-role-badge.iqa{background:rgba(251,191,36,.14);color:#fbbf24;border:1px solid rgba(251,191,36,.3)}
.sai-ma-wave-label{display:flex;align-items:center;gap:10px;margin:18px 0 12px;font-size:10.5px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;color:rgba(255,255,255,.35);opacity:0;transition:opacity .5s ease}
.sai-ma-wave-label.visible{opacity:1}
.sai-ma-wave-line{flex:1;height:1px;background:rgba(255,255,255,.08)}
.sai-ma-lanes{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.sai-ma-agent{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:14px 16px;opacity:0;transform:translateY(10px);transition:opacity .5s ease,transform .5s ease,border-color .4s,box-shadow .4s}
.sai-ma-agent.visible{opacity:1;transform:none}
.sai-ma-agent.done{border-color:rgba(52,211,153,.35);box-shadow:0 0 24px rgba(52,211,153,.08)}
.sai-ma-agent-head{display:flex;align-items:center;gap:9px;margin-bottom:12px;font-size:12.5px;font-weight:700;color:#e0e0f0}
.sai-ma-agent-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;background:#a78bfa;animation:sai-pulse 1.4s ease-in-out infinite}
.sai-ma-agent.done .sai-ma-agent-dot{background:#34d399;animation:none}
.sai-ma-agent-task{margin-left:auto;font-size:10px;font-weight:600;color:rgba(255,255,255,.35);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:55%}
.sai-ma-stage{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:7px;font-size:11.5px;color:rgba(255,255,255,.3);border-left:2px solid transparent;transition:all .35s ease}
.sai-ma-stage-ico{width:16px;text-align:center;flex-shrink:0;font-size:11px}
.sai-ma-stage-status{margin-left:auto;font-size:10px;font-weight:700;letter-spacing:.4px;flex-shrink:0}
.sai-ma-stage.active{background:rgba(124,106,247,.1);border-left-color:rgba(124,106,247,.5);color:#e0e0f0}
.sai-ma-stage.active .sai-ma-stage-status{color:#a78bfa;animation:sai-pulse 1.2s ease-in-out infinite}
.sai-ma-stage.pass{background:rgba(15,168,118,.07);border-left-color:rgba(15,168,118,.45);color:rgba(255,255,255,.75)}
.sai-ma-stage.pass .sai-ma-stage-status{color:#34d399;animation:none}
.sai-ma-integration{display:flex;align-items:center;gap:10px;margin-top:16px;padding:12px 14px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:10px;font-size:12px;color:rgba(255,255,255,.55);opacity:0;transform:translateY(8px);transition:opacity .5s ease,transform .5s ease,border-color .4s}
.sai-ma-integration.visible{opacity:1;transform:none}
.sai-ma-integration.pass{border-color:rgba(52,211,153,.35)}
.sai-ma-merge-badge{display:none;align-items:center;gap:6px;background:linear-gradient(135deg,rgba(124,106,247,.28),rgba(15,168,118,.22));border:1px solid rgba(52,211,153,.4);color:#34d399;padding:4px 12px;border-radius:6px;font-size:11px;font-weight:700;margin-left:auto;white-space:nowrap;animation:passGlow 1.8s ease-in-out infinite}
.sai-ma-merge-badge.visible{display:inline-flex}
.sai-ma-cursor{display:inline-block;width:7px;height:13px;background:#a78bfa;vertical-align:middle;margin-left:2px;border-radius:1px;animation:sai-blink 1s step-end infinite}
.sai-ma-bullets{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:56px}
.sai-ma-bullet{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:24px 22px;transition:border-color .3s,background .3s}
.sai-ma-bullet:hover{border-color:rgba(124,106,247,.4);background:rgba(124,106,247,.05)}
.sai-ma-bullet-icon{width:38px;height:38px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:17px;background:rgba(124,106,247,.12);border:1px solid rgba(124,106,247,.25);margin-bottom:14px}
.sai-ma-bullet h4{font-size:15px;font-weight:700;margin:0 0 6px;color:#fff}
.sai-ma-bullet p{font-size:13px;color:rgba(255,255,255,.5);line-height:1.55;margin:0}
@media(max-width:900px){
  .sai-ma-lanes{grid-template-columns:1fr}
  .sai-ma-bullets{grid-template-columns:1fr 1fr}
  .sai-ma-agent-task{display:none}
}
@media(max-width:600px){
  .sai-ma-bullets{grid-template-columns:1fr}
  .sai-ma-section{padding:70px 16px}
  .sai-ma-body{padding:16px 14px}
  .sai-ma-agent{padding:12px}
  /* Integration QA row: allow wrapping so the merge badge never overflows
     the viewport; badge drops to its own line instead of clipping. */
  .sai-ma-integration{flex-wrap:wrap;row-gap:8px}
  .sai-ma-merge-badge{margin-left:0}
  .sai-ma-supervisor{align-items:flex-start}
}
`;

/** One pipeline stage row inside an agent lane. */
function Stage({ id, icon, label }: { id: string; icon: string; label: string }) {
  return (
    <div className="sai-ma-stage" id={id}>
      <span className="sai-ma-stage-ico">{icon}</span>
      <span>{label}</span>
      <span className="sai-ma-stage-status">QUEUED</span>
    </div>
  );
}

export function MultiAgentShowcase() {
  const sectionRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const section = sectionRef.current;
    if (!section) return;

    let fired = false;
    const timers: number[] = [];
    const after = (ms: number, fn: () => void) => timers.push(window.setTimeout(fn, ms));

    // --- tiny DOM helpers, all null-safe ---
    const el = (id: string) => document.getElementById(id);
    const show = (id: string) => el(id)?.classList.add('visible');
    const setStage = (id: string, state: 'active' | 'pass', status: string) => {
      const node = el(id);
      if (!node) return;
      node.classList.remove('active', 'pass');
      node.classList.add(state);
      const s = node.querySelector('.sai-ma-stage-status');
      if (s) s.textContent = status;
    };

    function typeText(target: HTMLElement, text: string, speed: number, done: () => void) {
      const cursor = document.createElement('span');
      cursor.className = 'sai-ma-cursor';
      target.appendChild(cursor);
      let i = 0;
      function step() {
        if (i < text.length) {
          target.insertBefore(document.createTextNode(text[i++]), cursor);
          timers.push(window.setTimeout(step, speed));
        } else {
          cursor.remove();
          done();
        }
      }
      step();
    }

    // Runs one agent lane: architect -> surgeon -> qa, with a per-lane time offset.
    function runAgent(lane: 1 | 2, offset: number, done: () => void) {
      const a = `maA${lane}s1`, s = `maA${lane}s2`, q = `maA${lane}s3`;
      after(offset, () => setStage(a, 'active', 'PLANNING'));
      after(offset + 1400, () => { setStage(a, 'pass', '✓ PLAN'); setStage(s, 'active', 'EDITING'); });
      after(offset + 3200, () => { setStage(s, 'pass', '✓ APPLIED'); setStage(q, 'active', 'QA 0/10'); });
      after(offset + 4600, () => {
        setStage(q, 'pass', '✓ QA 10/10');
        el(`maAgent${lane}`)?.classList.add('done');
        done();
      });
    }

    function run() {
      const supText = el('maSupText');
      if (!supText) return;

      // 1) Supervisor plans the waves
      typeText(supText, 'analyzing scope · 4 tasks · grouped into file-disjoint waves', 16, () => {
        // 2) Wave label + both agent lanes appear
        after(400, () => show('maWaveLabel'));
        after(700, () => show('maAgent1'));
        after(950, () => show('maAgent2'));

        // 3) Both agents run their full pipelines in parallel (slightly offset)
        let finished = 0;
        const onAgentDone = () => {
          finished += 1;
          if (finished < 2) return;
          // 4) Integration QA reviews the combined result
          after(500, () => {
            show('maIntegration');
            const iqaText = el('maIqaText');
            if (!iqaText) return;
            typeText(iqaText, 'scanning combined diff for cross-task conflicts…', 18, () => {
              after(900, () => {
                el('maIntegration')?.classList.add('pass');
                el('maMergeBadge')?.classList.add('visible');
              });
            });
          });
        };
        runAgent(1, 1100, onAgentDone);
        runAgent(2, 1700, onAgentDone);
      });
    }

    const obs = new IntersectionObserver((entries) => {
      entries.forEach(e => {
        if (e.isIntersecting && !fired) {
          fired = true;
          obs.disconnect();
          run();
        }
      });
    }, { threshold: 0.3 });
    obs.observe(section);

    return () => {
      obs.disconnect();
      timers.forEach(t => window.clearTimeout(t));
    };
  }, []);

  return (
    <section className="sai-ma-section" id="multi-agent" ref={sectionRef}>
      <style>{CSS}</style>
      <div className="sai-ma-wrap">
        <div className="sai-ma-head">
          <div className="sai-ma-pill">
            <span className="sai-ma-pill-dot"></span>
            True Multi-Agent
          </div>
          <h2 className="sai-ma-h2">One prompt.<br/><span className="ma-grad">A whole team of agents.</span></h2>
          <p className="sai-ma-sub">Most AI tools run one model, one thread, one task at a time. SurgicalAI deploys a coordinated team — a supervisor plans the work, parallel agents execute it, and an integration reviewer signs off on the combined result.</p>
        </div>

        {/* Mission control console */}
        <div className="sai-ma-console">
          <div className="sai-ma-bar">
            <div className="sai-dot sai-dot-r"></div>
            <div className="sai-dot sai-dot-y"></div>
            <div className="sai-dot sai-dot-g"></div>
            <span className="sai-ma-bar-title">SurgicalAI — Agent Orchestrator</span>
          </div>
          <div className="sai-ma-body">
            <div className="sai-ma-supervisor">
              <span className="sai-ma-role-badge sup">Supervisor</span>
              <span id="maSupText"></span>
            </div>

            <div className="sai-ma-wave-label" id="maWaveLabel">
              Wave 1 — 2 agents in parallel
              <span className="sai-ma-wave-line"></span>
            </div>

            <div className="sai-ma-lanes">
              <div className="sai-ma-agent" id="maAgent1">
                <div className="sai-ma-agent-head">
                  <span className="sai-ma-agent-dot"></span>
                  Agent 1
                  <span className="sai-ma-agent-task">auth/useSession.ts</span>
                </div>
                <Stage id="maA1s1" icon="◆" label="Architect · plan the edit" />
                <Stage id="maA1s2" icon="⚡" label="Surgeon · apply precise diff" />
                <Stage id="maA1s3" icon="🛡" label="QA · verify & score" />
              </div>
              <div className="sai-ma-agent" id="maAgent2">
                <div className="sai-ma-agent-head">
                  <span className="sai-ma-agent-dot"></span>
                  Agent 2
                  <span className="sai-ma-agent-task">api/billing.py</span>
                </div>
                <Stage id="maA2s1" icon="◆" label="Architect · plan the edit" />
                <Stage id="maA2s2" icon="⚡" label="Surgeon · apply precise diff" />
                <Stage id="maA2s3" icon="🛡" label="QA · verify & score" />
              </div>
            </div>

            <div className="sai-ma-integration" id="maIntegration">
              <span className="sai-ma-role-badge iqa">Integration QA</span>
              <span id="maIqaText"></span>
              <span className="sai-ma-merge-badge" id="maMergeBadge">✓ No conflicts · Merged clean</span>
            </div>
          </div>
        </div>

        {/* Why it matters */}
        <div className="sai-ma-bullets">
          <div className="sai-ma-bullet">
            <div className="sai-ma-bullet-icon">🧭</div>
            <h4>Wave planning</h4>
            <p>A supervisor groups tasks into file-disjoint waves, so parallel agents never touch the same code.</p>
          </div>
          <div className="sai-ma-bullet">
            <div className="sai-ma-bullet-icon">🤖</div>
            <h4>Full pipeline per agent</h4>
            <p>Every agent runs the complete Architect → Surgeon → QA pipeline — not a stripped-down worker.</p>
          </div>
          <div className="sai-ma-bullet">
            <div className="sai-ma-bullet-icon">🛡️</div>
            <h4>Integration QA</h4>
            <p>A dedicated reviewer inspects the combined result for cross-task conflicts before anything lands.</p>
          </div>
          <div className="sai-ma-bullet">
            <div className="sai-ma-bullet-icon">⚛️</div>
            <h4>Zero double-work</h4>
            <p>Atomic task claims guarantee no two agents ever pick up the same job — even at full parallelism.</p>
          </div>
        </div>
      </div>
    </section>
  );
}
