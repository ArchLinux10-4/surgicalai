/**
 * LandingPage — SurgicalAI public marketing homepage
 * Shown at "/" for unauthenticated users.
 * Pure CSS — no JS interactions, works on all devices.
 *
 * Stripe wiring notes (future):
 *  - Starter plan button: data-stripe-price-id="price_starter_monthly"
 *  - Pro plan button:     data-stripe-price-id="price_pro_monthly"
 *  - Hook: add onClick={() => stripeCheckout(priceId)} to .sai-pricing-cta buttons
 *  - Backend: POST /billing/create-checkout-session { priceId, userId }
 */
import { useEffect, useState } from 'react';
import LoginIcon from '@mui/icons-material/Login';

const CSS = `
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#ffffff;
  --bg2:#f6f7fb;
  --bg3:#eef0f7;
  --surface:#e8ebf5;
  --border:#e2e5ef;
  --border2:#d4d8e8;
  --accent:#6d5ce6;
  --accent2:#7c6af7;
  --green:#0fa876;
  --red:#dc2626;
  --amber:#d97706;
  --txt:#0e0e1a;
  --txt2:#4a4a6a;
  --txt3:#8888a8;
  --radius:12px;
  --font:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
  --e-bg:#0d0d17;
  --e-bg2:#111120;
  --e-bg3:#161628;
  --e-border:#ffffff10;
  --e-txt:#e0e0f0;
  --e-txt2:#7070a0;
  --e-txt3:#404060;
}
html{scroll-behavior:smooth}

/* NAV */
.sai-nav{
  position:fixed;top:0;left:0;right:0;z-index:100;
  display:flex;align-items:center;justify-content:space-between;
  padding:0 48px;height:64px;
  background:rgba(255,255,255,0.88);
  backdrop-filter:blur(16px);
  border-bottom:1px solid var(--border);
}
.sai-nav-logo{display:flex;align-items:center;gap:10px;font-size:18px;font-weight:700;letter-spacing:-0.3px;text-decoration:none;color:var(--txt)}
.sai-nav-logo svg{width:28px;height:28px}
.sai-nav-links{display:flex;gap:32px;list-style:none}
.sai-nav-links a{color:var(--txt2);text-decoration:none;font-size:14px;transition:color .2s}
.sai-nav-links a:hover{color:var(--txt)}
.sai-nav-cta{
  background:var(--accent);color:#fff;border:none;
  padding:9px 20px;border-radius:8px;font-size:14px;font-weight:600;
  cursor:pointer;text-decoration:none;transition:opacity .2s;
  display:inline-flex;align-items:center;gap:6px;
}
.sai-nav-cta:hover{opacity:.85}

/* HERO */
.sai-hero{
  min-height:100vh;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:100px 24px 80px;
  text-align:center;
  position:relative;overflow:hidden;
  background:linear-gradient(180deg,#f0effe 0%,#ffffff 55%);
}
.sai-hero-glow{
  position:absolute;top:-100px;left:50%;transform:translateX(-50%);
  width:900px;height:600px;
  background:radial-gradient(ellipse at center,rgba(124,106,247,.13) 0%,transparent 65%);
  pointer-events:none;
}
.sai-hero-badge{
  display:inline-flex;align-items:center;gap:8px;
  background:rgba(109,92,230,.1);border:1px solid rgba(109,92,230,.25);
  border-radius:999px;padding:6px 16px;font-size:12px;font-weight:600;
  color:var(--accent);letter-spacing:.5px;text-transform:uppercase;margin-bottom:28px;
}
.sai-hero-badge-dot{width:6px;height:6px;border-radius:50%;background:var(--accent);animation:sai-pulse 2s infinite}
@keyframes sai-pulse{0%,100%{opacity:1}50%{opacity:.4}}
.sai-h1{
  font-size:clamp(40px,6vw,80px);font-weight:800;letter-spacing:-2px;line-height:1.05;
  max-width:900px;margin-bottom:24px;color:var(--txt);
}
.sai-h1 span{
  background:linear-gradient(135deg,#6d5ce6,#7c6af7,#0fa876);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.sai-hero-sub{font-size:clamp(16px,2vw,20px);color:var(--txt2);max-width:560px;margin:0 auto 48px;line-height:1.7}
.sai-hero-actions{display:flex;gap:16px;justify-content:center;flex-wrap:wrap;margin-bottom:64px}
.sai-btn-primary{
  background:var(--accent);color:#fff;padding:14px 32px;border-radius:10px;
  font-size:15px;font-weight:700;text-decoration:none;transition:transform .2s,opacity .2s;
  display:inline-flex;align-items:center;gap:8px;
}
.sai-btn-primary:hover{opacity:.9;transform:translateY(-1px)}
.sai-btn-secondary{
  background:#fff;color:var(--txt);padding:14px 32px;border-radius:10px;
  font-size:15px;font-weight:600;text-decoration:none;border:1.5px solid var(--border2);
  transition:border-color .2s,background .2s;
}
.sai-btn-secondary:hover{border-color:var(--accent2);background:rgba(109,92,230,.04)}

/* MOCKUP */
.sai-hero-mockup{
  width:100%;max-width:1100px;margin:0 auto;
  background:var(--e-bg2);border:1px solid rgba(255,255,255,.1);border-radius:16px;
  overflow:hidden;
  box-shadow:0 4px 6px rgba(0,0,0,.06),0 12px 40px rgba(0,0,0,.12),0 40px 100px rgba(109,92,230,.15),0 0 0 1px rgba(255,255,255,.08);
}
.sai-mockup-titlebar{
  height:40px;background:var(--e-bg3);border-bottom:1px solid var(--e-border);
  display:flex;align-items:center;padding:0 16px;gap:8px;
}
.sai-dot{width:12px;height:12px;border-radius:50%}
.sai-dot-r{background:#ff5f57}.sai-dot-y{background:#febc2e}.sai-dot-g{background:#28c840}
.sai-titlebar-text{margin-left:12px;font-size:12px;color:var(--e-txt2);font-weight:500}
.sai-mockup-body{display:grid;grid-template-columns:220px 1fr 300px;min-height:500px}
.sai-mock-sidebar{background:var(--e-bg3);border-right:1px solid var(--e-border);padding:16px 0}
.sai-sidebar-section{padding:8px 16px;font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--e-txt3);margin-bottom:4px}
.sai-mock-file{display:flex;align-items:center;gap:8px;padding:6px 16px;font-size:12px;color:var(--e-txt2)}
.sai-mock-file.active{background:rgba(124,106,247,.1);color:var(--e-txt)}
.sai-mock-file.active .sai-file-icon{opacity:1;color:#a78bfa}
.sai-file-icon{width:14px;height:14px;opacity:.5;flex-shrink:0}
.sai-mock-editor{background:var(--e-bg2);position:relative;overflow:hidden;display:flex;flex-direction:column}
.sai-editor-tabs{height:36px;background:var(--e-bg3);border-bottom:1px solid var(--e-border);display:flex;align-items:flex-end;padding:0 12px}
.sai-mock-tab{font-size:12px;padding:6px 14px;color:var(--e-txt3);border-radius:6px 6px 0 0;border:1px solid transparent;border-bottom:none}
.sai-mock-tab.active{background:var(--e-bg2);color:var(--e-txt);border-color:var(--e-border);border-bottom-color:var(--e-bg2)}
.sai-mock-code{flex:1;padding:16px;font-family:'Fira Code','Cascadia Code',monospace;font-size:12px;line-height:1.7;overflow:hidden}
.sai-line{display:flex;gap:12px;min-height:20px}
.sai-ln{color:var(--e-txt3);width:24px;text-align:right;flex-shrink:0;user-select:none;font-size:11px}
.sai-ct{white-space:pre;color:var(--e-txt)}
.sai-kw{color:#a78bfa}.sai-fn{color:#60a5fa}.sai-str{color:#34d399}.sai-cm{color:#4b6080}.sai-ty{color:#fbbf24}.sai-op{color:#7090b0}
.sai-line.del{background:rgba(248,113,113,.08);border-left:2px solid rgba(248,113,113,.4)}
.sai-line.add{background:rgba(15,168,118,.08);border-left:2px solid rgba(15,168,118,.4)}
.sai-line.del .sai-ct{color:rgba(248,113,113,.85)}
.sai-line.add .sai-ct{color:rgba(52,211,153,.9)}
@keyframes sai-fade-in{from{opacity:0;transform:translateX(-6px)}to{opacity:1;transform:none}}
.sai-line.del{animation:sai-fade-in .4s ease both}
.sai-line.add{animation:sai-fade-in .4s ease .25s both}
.sai-mock-panel{background:var(--e-bg3);border-left:1px solid var(--e-border);display:flex;flex-direction:column}
.sai-panel-header{height:36px;border-bottom:1px solid var(--e-border);display:flex;align-items:center;padding:0 14px;gap:8px;font-size:12px;font-weight:600;color:var(--e-txt2)}
.sai-panel-badge{font-size:10px;padding:2px 8px;border-radius:999px;font-weight:700;letter-spacing:.3px}
.sai-badge-arch{background:rgba(124,106,247,.25);color:#a78bfa}
.sai-badge-surg{background:rgba(15,168,118,.2);color:#34d399}
.sai-mock-messages{flex:1;padding:12px;overflow:hidden;display:flex;flex-direction:column;gap:10px}
.sai-msg{font-size:12px;line-height:1.5;padding:10px 12px;border-radius:10px;max-width:100%}
.sai-msg-user{background:rgba(124,106,247,.15);border:1px solid rgba(124,106,247,.25);color:var(--e-txt);align-self:flex-end}
.sai-msg-arch{background:rgba(255,255,255,.04);border:1px solid var(--e-border);color:var(--e-txt2)}
.sai-msg-step{display:flex;align-items:flex-start;gap:8px;padding:8px 10px;background:rgba(15,168,118,.07);border:1px solid rgba(15,168,118,.2);border-radius:8px}
.sai-step-icon{font-size:14px;margin-top:1px;flex-shrink:0}
.sai-step-text{font-size:11px;color:var(--e-txt2)}
.sai-step-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#34d399;display:block;margin-bottom:2px}
@keyframes sai-blink{0%,100%{opacity:1}50%{opacity:0}}
.sai-cursor-blink{display:inline-block;width:2px;height:13px;background:#a78bfa;vertical-align:middle;animation:sai-blink 1s step-end infinite}
.sai-mock-input-bar{padding:10px 12px;border-top:1px solid var(--e-border);display:flex;gap:8px;align-items:center}
.sai-mock-input{flex:1;background:rgba(255,255,255,.06);border:1px solid var(--e-border);border-radius:7px;padding:7px 10px;font-size:11px;color:var(--e-txt3);font-family:inherit}
@keyframes sai-type-in{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0% 0 0)}}
.sai-typed{animation:sai-type-in 1.2s steps(30) 1s both}
.sai-typed2{animation:sai-type-in 1s steps(25) 2.5s both}
.sai-typed3{animation:sai-type-in .8s steps(20) 4s both}

/* SECTION SHARED */
.sai-section{padding:100px 24px}
.sai-container{max-width:1100px;margin:0 auto}
.sai-section-label{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--accent);margin-bottom:16px}
.sai-section-title{font-size:clamp(28px,4vw,48px);font-weight:800;letter-spacing:-1px;line-height:1.1;margin-bottom:20px;color:var(--txt)}
.sai-section-sub{font-size:16px;color:var(--txt2);max-width:560px;line-height:1.7}

/* STATS */
.sai-stats-strip{
  display:grid;grid-template-columns:repeat(4,1fr);gap:0;
  border:1.5px solid var(--border2);border-radius:16px;overflow:hidden;
  background:#fff;box-shadow:0 2px 12px rgba(0,0,0,.05);
}
.sai-stat{padding:36px 28px;text-align:center;border-right:1px solid var(--border)}
.sai-stat:last-child{border-right:none}
.sai-stat-num{font-size:42px;font-weight:800;letter-spacing:-2px;color:var(--txt);line-height:1}
.sai-stat-num span{color:var(--accent)}
.sai-stat-label{font-size:13px;color:var(--txt2);margin-top:8px}

/* PIPELINE */
.sai-pipeline-grid{display:grid;grid-template-columns:1fr 1fr;gap:60px;align-items:center}
.sai-pipeline-steps{display:flex;flex-direction:column;gap:0}
.sai-pipe-step{display:flex;gap:20px;padding:24px 0;border-bottom:1px solid var(--border)}
.sai-pipe-step:last-child{border-bottom:none}
.sai-pipe-step-num{
  width:36px;height:36px;border-radius:10px;flex-shrink:0;margin-top:2px;
  background:rgba(109,92,230,.1);border:1.5px solid rgba(109,92,230,.25);
  display:flex;align-items:center;justify-content:center;
  font-size:13px;font-weight:800;color:var(--accent);
}
.sai-pipe-step h3{font-size:16px;font-weight:700;margin-bottom:6px;color:var(--txt)}
.sai-pipe-step p{font-size:14px;color:var(--txt2);line-height:1.6}
.sai-pipe-tag{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;padding:3px 8px;border-radius:5px;margin-top:8px}
.sai-tag-arch{background:rgba(109,92,230,.1);color:var(--accent)}
.sai-tag-surg{background:rgba(15,168,118,.1);color:var(--green)}
.sai-tag-qa{background:rgba(217,119,6,.1);color:var(--amber)}
.sai-pipeline-visual{position:relative}
.sai-pipe-card{
  background:#fff;border:1.5px solid var(--border2);border-radius:14px;
  padding:20px;margin-bottom:12px;position:relative;overflow:hidden;
  box-shadow:0 2px 8px rgba(0,0,0,.04);
}
.sai-pipe-card::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(to bottom,var(--accent),var(--green))}
.sai-pipe-card-label{font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--txt3);margin-bottom:8px}
.sai-pipe-card-title{font-size:14px;font-weight:700;color:var(--txt);margin-bottom:4px}
.sai-pipe-card-desc{font-size:12px;color:var(--txt2);line-height:1.5}
.sai-pipe-connector{width:2px;height:16px;background:linear-gradient(to bottom,var(--accent),var(--green));margin:0 auto;opacity:.3}

/* FEATURES */
.sai-features-grid{
  display:grid;grid-template-columns:repeat(3,1fr);gap:1px;
  background:var(--border);border-radius:16px;overflow:hidden;border:1.5px solid var(--border2);
}
.sai-feat-card{background:#fff;padding:32px 28px;transition:background .2s}
.sai-feat-card:hover{background:var(--bg2)}
.sai-feat-icon{
  width:44px;height:44px;border-radius:12px;
  background:rgba(109,92,230,.08);border:1.5px solid rgba(109,92,230,.18);
  display:flex;align-items:center;justify-content:center;
  font-size:20px;margin-bottom:18px;
}
.sai-feat-card h3{font-size:16px;font-weight:700;margin-bottom:8px;color:var(--txt)}
.sai-feat-card p{font-size:14px;color:var(--txt2);line-height:1.6}

/* DIFF */
.sai-diff-section{background:var(--e-bg2);border-radius:16px;border:1px solid rgba(255,255,255,.08);overflow:hidden;box-shadow:0 8px 32px rgba(0,0,0,.12)}
.sai-diff-header{padding:14px 20px;background:var(--e-bg3);border-bottom:1px solid var(--e-border);display:flex;align-items:center;gap:12px;font-size:13px;font-weight:600}
.sai-diff-filename{color:var(--e-txt);font-family:monospace}
.sai-diff-badge{font-size:11px;padding:3px 10px;border-radius:6px;font-weight:700}
.sai-diff-badge.surg{background:rgba(15,168,118,.15);color:#34d399}
.sai-diff-body{padding:20px;font-family:'Fira Code',monospace;font-size:13px;line-height:1.8}
.sai-diff-line{display:flex;gap:14px;padding:1px 8px;border-radius:4px}
.sai-diff-line.minus{background:rgba(248,113,113,.08)}
.sai-diff-line.plus{background:rgba(15,168,118,.08)}
.sai-diff-sign{width:16px;flex-shrink:0;font-weight:700}
.sai-diff-sign.m{color:#f87171}.sai-diff-sign.p{color:#34d399}.sai-diff-sign.n{color:var(--e-txt3)}
.sai-diff-code{color:var(--e-txt)}
.sai-diff-line.minus .sai-diff-code{color:rgba(248,113,113,.85)}
.sai-diff-line.plus .sai-diff-code{color:rgba(52,211,153,.9)}

/* INTEGRATIONS */
.sai-integ-grid{display:grid;grid-template-columns:1fr 1fr;gap:64px;align-items:center}
.sai-integ-grid.reverse{direction:rtl}
.sai-integ-grid.reverse > *{direction:ltr}
.sai-integ-mockup{
  background:var(--e-bg2);border:1px solid rgba(255,255,255,.08);
  border-radius:16px;overflow:hidden;
  box-shadow:0 8px 40px rgba(0,0,0,.14),0 0 0 1px rgba(255,255,255,.05);
}
.sai-integ-titlebar{
  height:38px;background:var(--e-bg3);border-bottom:1px solid var(--e-border);
  display:flex;align-items:center;padding:0 14px;gap:8px;
}
.sai-integ-tbtitle{margin-left:8px;font-size:12px;color:var(--e-txt2);font-weight:600}
.sai-integ-body{padding:14px;display:flex;flex-direction:column;gap:6px}
.sai-integ-row{
  display:flex;align-items:center;gap:10px;
  padding:9px 12px;background:rgba(255,255,255,.03);
  border:1px solid var(--e-border);border-radius:9px;
  font-size:12px;color:var(--e-txt2);
}
.sai-integ-row.active{background:rgba(124,106,247,.07);border-color:rgba(124,106,247,.2);color:var(--e-txt)}
.sai-integ-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.sai-integ-dot.green{background:#34d399}
.sai-integ-dot.purple{background:#a78bfa}
.sai-integ-dot.gray{background:#404060}
.sai-integ-dot.blue{background:#60a5fa}
.sai-integ-dot.amber{background:#fbbf24}
.sai-integ-meta{font-size:10px;color:var(--e-txt3);margin-left:auto;white-space:nowrap}
.sai-integ-badge-sm{font-size:10px;padding:2px 7px;border-radius:5px;font-weight:700;margin-left:auto;white-space:nowrap}
.sai-integ-badge-sm.merged{background:rgba(124,106,247,.2);color:#a78bfa}
.sai-integ-badge-sm.open{background:rgba(15,168,118,.15);color:#34d399}
.sai-integ-badge-sm.review{background:rgba(251,191,36,.15);color:#fbbf24}
.sai-integ-badge-sm.inprog{background:rgba(96,165,250,.15);color:#60a5fa}
.sai-integ-badge-sm.done{background:rgba(15,168,118,.15);color:#34d399}
.sai-integ-badge-sm.todo{background:rgba(255,255,255,.06);color:#7070a0}
.sai-integ-badge-sm.cancelled{background:rgba(248,113,113,.1);color:#f87171}
.sai-integ-section-divider{height:1px;background:var(--e-border);margin:4px 0}
.sai-integ-section-label{font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--e-txt3);padding:4px 12px 0}
.sai-integ-bullets{display:flex;flex-direction:column;gap:16px;margin-top:32px}
.sai-integ-bullet{display:flex;align-items:flex-start;gap:14px}
.sai-integ-bullet-icon{
  width:36px;height:36px;border-radius:10px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;font-size:16px;
  background:rgba(109,92,230,.08);border:1.5px solid rgba(109,92,230,.15);
}
.sai-integ-bullet h4{font-size:15px;font-weight:700;margin-bottom:4px;color:var(--txt)}
.sai-integ-bullet p{font-size:14px;color:var(--txt2);line-height:1.5}
.sai-integ-logo-badge{
  display:inline-flex;align-items:center;gap:8px;
  background:var(--bg3);border:1.5px solid var(--border2);
  border-radius:999px;padding:6px 16px 6px 10px;
  font-size:13px;font-weight:700;color:var(--txt);
  margin-bottom:20px;
}

/* QA SHOWCASE */
.sai-qa-section{padding:100px 24px;background:#f4f3ff;position:relative;overflow:hidden}
.sai-qa-section::before{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:radial-gradient(ellipse 55% 60% at 80% 40%,rgba(109,92,230,.07) 0%,transparent 70%);pointer-events:none}
.sai-qa-section::after{content:'';position:absolute;top:0;left:0;right:0;bottom:0;background:radial-gradient(ellipse 40% 40% at 15% 90%,rgba(10,168,118,.06) 0%,transparent 65%);pointer-events:none}
.sai-qa-wrap{max-width:1100px;margin:0 auto;position:relative;z-index:1}
.sai-qa-head{text-align:center;margin-bottom:72px}
.sai-qa-pill{display:inline-flex;align-items:center;gap:6px;background:rgba(109,92,230,.1);border:1px solid rgba(109,92,230,.3);color:#6d5ce6;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:6px 16px;border-radius:100px;margin-bottom:24px}
.sai-qa-pill-dot{width:6px;height:6px;border-radius:50%;background:#6d5ce6;animation:qaPulse 2s ease-in-out infinite}
@keyframes qaPulse{0%,100%{opacity:1}50%{opacity:.35}}
.sai-qa-h2{font-size:clamp(36px,5vw,58px);font-weight:800;letter-spacing:-1.5px;line-height:1.08;color:#0f0d1a;margin:0 0 20px}
.sai-qa-h2 .grad{background:linear-gradient(135deg,#6d5ce6 0%,#0aa876 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sai-qa-sub{font-size:18px;color:#4b4768;max-width:580px;margin:0 auto;line-height:1.65}
.sai-qa-body{display:grid;grid-template-columns:380px 1fr;gap:56px;align-items:start;margin-bottom:72px}
.sai-qa-steps{display:flex;flex-direction:column}
.sai-qa-step{display:flex;gap:16px;padding:20px 0;position:relative;transition:opacity .3s}
.sai-qa-step-num{width:38px;height:38px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;letter-spacing:.03em;border:1px solid rgba(109,92,230,.35);background:#fff;color:#6d5ce6;box-shadow:0 2px 8px rgba(109,92,230,.12);transition:all .3s}
.sai-qa-step.is-pass .sai-qa-step-num{border-color:rgba(10,168,118,.4);background:#fff;color:#0aa876;font-size:16px;box-shadow:0 2px 8px rgba(10,168,118,.15)}
.sai-qa-step-text strong{display:block;color:#0f0d1a;font-size:15px;margin-bottom:5px;font-weight:600}
.sai-qa-step-text p{color:#4b4768;font-size:13.5px;margin:0;line-height:1.55}
.sai-qa-step-text code{background:rgba(109,92,230,.1);color:#6d5ce6;padding:2px 6px;border-radius:4px;font-family:'JetBrains Mono',monospace;font-size:11.5px;border:1px solid rgba(109,92,230,.2)}
.sai-qa-connector{width:1px;height:20px;background:linear-gradient(to bottom,rgba(109,92,230,.3),rgba(109,92,230,.1));margin-left:18px}
.sai-qa-step.qa-step-active .sai-qa-step-num{background:linear-gradient(135deg,#6d5ce6,#8b76f0);color:#fff;border-color:#6d5ce6;box-shadow:0 0 0 3px rgba(109,92,230,.18),0 2px 10px rgba(109,92,230,.35);transform:scale(1.08)}
.sai-qa-step.qa-step-done .sai-qa-step-num{background:linear-gradient(135deg,#0aa876,#34d399);color:#fff;border-color:#0aa876;box-shadow:0 0 0 3px rgba(10,168,118,.18),0 2px 10px rgba(10,168,118,.28)}
.sai-qa-terminal{background:#1a1333;border:1px solid rgba(109,92,230,.25);border-radius:14px;overflow:hidden;box-shadow:0 24px 64px rgba(109,92,230,.18),0 4px 16px rgba(0,0,0,.12),0 0 0 1px rgba(109,92,230,.08)}
.sai-term-bar{height:38px;background:#120d24;border-bottom:1px solid rgba(255,255,255,.06);display:flex;align-items:center;padding:0 14px;gap:7px}
.sai-term-btn{width:12px;height:12px;border-radius:50%;flex-shrink:0}
.sai-term-btn.r{background:#ff5f57}.sai-term-btn.y{background:#febc2e}.sai-term-btn.g{background:#28c840}
.sai-term-title{margin-left:10px;font-size:12px;color:rgba(255,255,255,.3);font-weight:500;font-family:'JetBrains Mono',monospace}
.sai-term-body{padding:18px 20px;min-height:320px;max-height:400px;overflow-y:auto;display:flex;flex-direction:column;gap:3px;font-family:'JetBrains Mono','Fira Code',monospace;font-size:12.5px;line-height:1.6}
.sai-tl{display:block;white-space:pre-wrap;word-break:break-all}
.sai-tl.cmd{color:#a78bfa;font-weight:600}
.sai-tl.ok{color:#34d399}
.sai-tl.err{color:#f87171}
.sai-tl.warn{color:#fbbf24}
.sai-tl.info{color:#60a5fa}
.sai-tl.dim{color:rgba(255,255,255,.45)}
.sai-tl.blank{height:6px}
.sai-tl.divider{height:1px;background:rgba(255,255,255,.08);margin:4px 0}
.sai-score-fail{display:inline-flex;align-items:center;gap:5px;background:rgba(248,113,113,.15);border:1px solid rgba(248,113,113,.3);color:#f87171;padding:2px 10px;border-radius:6px;font-size:11.5px;font-weight:700}
.sai-score-pass{display:inline-flex;align-items:center;gap:5px;background:rgba(52,211,153,.12);border:1px solid rgba(52,211,153,.3);color:#34d399;padding:3px 12px;border-radius:6px;font-size:12px;font-weight:700;animation:passGlow 1.8s ease-in-out infinite}
@keyframes passGlow{0%,100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}50%{box-shadow:0 0 12px 3px rgba(52,211,153,.25)}}
.qa-cursor{display:inline-block;width:7px;height:13px;background:#a78bfa;vertical-align:middle;margin-left:1px;animation:qaBlinkCursor .7s step-end infinite}
@keyframes qaBlinkCursor{0%,100%{opacity:1}50%{opacity:0}}
.sai-qa-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:0;border-top:1px solid rgba(109,92,230,.15);padding-top:56px}
.sai-qa-stat{text-align:center;padding:0 16px;position:relative}
.sai-qa-stat+.sai-qa-stat::before{content:'';position:absolute;left:0;top:25%;height:50%;width:1px;background:rgba(109,92,230,.15)}
.sai-qa-stat-n{display:block;font-size:52px;font-weight:800;letter-spacing:-2px;background:linear-gradient(135deg,#6d5ce6 0%,#0aa876 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;line-height:1;margin-bottom:10px}
.sai-qa-stat-l{font-size:13.5px;color:#4b4768;line-height:1.4}
@media(max-width:900px){.sai-qa-body{grid-template-columns:1fr}.sai-qa-stats{grid-template-columns:repeat(2,1fr);gap:32px}.sai-qa-stat+.sai-qa-stat::before{display:none}}

/* COMPARE */
.sai-compare-grid{display:grid;grid-template-columns:1fr 1fr;gap:32px}
.sai-compare-card{background:#fff;border:1.5px solid var(--border2);border-radius:16px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.04)}
.sai-compare-card.ours{border-color:rgba(109,92,230,.4);box-shadow:0 4px 24px rgba(109,92,230,.12)}
.sai-compare-header{padding:16px 24px;background:var(--bg2);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.sai-compare-header h3{font-size:15px;font-weight:700;color:var(--txt)}
.sai-compare-logo{font-size:12px;padding:3px 10px;border-radius:6px;font-weight:700;background:var(--bg3);color:var(--txt2)}
.sai-compare-logo.ours{background:rgba(109,92,230,.12);color:var(--accent)}
.sai-compare-list{padding:20px 24px;display:flex;flex-direction:column;gap:12px}
.sai-compare-item{display:flex;align-items:flex-start;gap:12px;font-size:14px}
.sai-compare-item .chk{font-size:16px;flex-shrink:0;margin-top:1px}
.chk.yes{color:var(--green)}.chk.no{color:var(--red)}.chk.partial{color:var(--amber)}
.sai-compare-item p{color:var(--txt2);line-height:1.5}
.sai-compare-item strong{color:var(--txt)}

/* PRICING */
.sai-pricing-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px;max-width:860px;margin:0 auto}
.sai-pricing-card{background:#fff;border:1.5px solid var(--border2);border-radius:20px;overflow:hidden;transition:transform .2s;box-shadow:0 2px 12px rgba(0,0,0,.04)}
.sai-pricing-card.featured{border-color:rgba(109,92,230,.4);box-shadow:0 8px 40px rgba(109,92,230,.14);transform:translateY(-4px)}
.sai-pricing-top{padding:32px 32px 24px}
.sai-pricing-popular{font-size:11px;font-weight:800;letter-spacing:.4px;text-transform:uppercase;padding:4px 12px;background:rgba(109,92,230,.12);color:var(--accent);border-radius:999px;display:inline-block;margin-bottom:16px}
.sai-pricing-tier{font-size:11px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;margin-bottom:12px;color:var(--txt2)}
.sai-pricing-price{font-size:52px;font-weight:800;letter-spacing:-2px;color:var(--txt);line-height:1;margin-bottom:6px;display:flex;align-items:flex-start;gap:2px}
.sai-pricing-price sup{font-size:22px;vertical-align:top;margin-top:12px;font-weight:700}
.sai-pricing-price sub{font-size:14px;font-weight:500;color:var(--txt2);letter-spacing:0;align-self:flex-end;margin-bottom:8px}
.sai-pricing-desc{font-size:14px;color:var(--txt2);margin-bottom:20px;line-height:1.6}
.sai-pricing-model{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;padding:5px 12px;border-radius:7px;margin-bottom:4px}
.sai-pricing-model.sonnet{background:rgba(96,165,250,.1);color:#60a5fa;border:1px solid rgba(96,165,250,.2)}
.sai-pricing-model.opus{background:rgba(109,92,230,.1);color:var(--accent);border:1px solid rgba(109,92,230,.2)}
.sai-pricing-divider{height:1px;background:var(--border);margin:0}
.sai-pricing-features{padding:24px 32px 32px;display:flex;flex-direction:column;gap:11px}
.sai-pricing-feature{display:flex;align-items:flex-start;gap:10px;font-size:14px}
.sai-pricing-feature .ck{color:var(--green);flex-shrink:0;margin-top:1px;font-size:15px}
.sai-pricing-feature p{color:var(--txt2);line-height:1.4}
.sai-pricing-feature strong{color:var(--txt)}
.sai-pricing-cta{
  width:100%;padding:13px;border-radius:10px;
  font-size:15px;font-weight:700;cursor:pointer;border:none;
  transition:opacity .2s,background .2s;text-decoration:none;
  display:block;text-align:center;margin-top:12px;
}
.sai-pricing-cta.starter-btn{background:var(--bg3);color:var(--txt);border:1.5px solid var(--border2)}
.sai-pricing-cta.starter-btn:hover{background:var(--bg2)}
.sai-pricing-cta.pro-btn{background:var(--accent);color:#fff}
.sai-pricing-cta.pro-btn:hover{opacity:.88}
.sai-pricing-note{text-align:center;margin-top:24px;font-size:13px;color:var(--txt3)}

/* CTA */
.sai-cta-section{padding:100px 24px;background:#0f0d1a}
.sai-cta-inner{max-width:1080px;margin:0 auto;display:grid;grid-template-columns:1fr 1fr;gap:80px;align-items:center}
.sai-cta-copy{color:#fff}
.sai-cta-copy h2{font-size:clamp(28px,3.5vw,44px);font-weight:800;letter-spacing:-1px;margin-bottom:20px;line-height:1.15}
.sai-cta-copy p{color:rgba(255,255,255,.6);font-size:16px;line-height:1.75;margin-bottom:32px}
.sai-cta-detail{display:flex;align-items:center;gap:10px;font-size:14px;color:rgba(255,255,255,.5);margin-bottom:12px}
.sai-cta-detail svg{flex-shrink:0;color:#6d5ce6}
.sai-form-card{background:#1a1630;border:1px solid rgba(109,92,230,.25);border-radius:20px;padding:36px 32px;box-shadow:0 24px 60px rgba(0,0,0,.4)}
.sai-contact-form{display:flex;flex-direction:column;gap:20px}
.sai-form-success{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:48px 24px;gap:16px;min-height:320px}
.sai-form-success-icon{width:72px;height:72px;border-radius:50%;background:rgba(15,168,118,0.12);display:flex;align-items:center;justify-content:center}
.sai-form-success h3{font-size:22px;font-weight:700;color:var(--txt);margin:0}
.sai-form-success p{color:var(--muted);font-size:15px;margin:0}
.sai-form-reset{margin-top:8px;background:none;border:1px solid rgba(109,92,230,0.4);color:#6d5ce6;border-radius:8px;padding:10px 22px;font-size:14px;cursor:pointer;transition:all .2s}
.sai-form-reset:hover{background:rgba(109,92,230,0.08)}
.sai-form-error{color:#e05c5c;font-size:13px;margin:0;padding:10px 14px;background:rgba(224,92,92,0.08);border-radius:8px;border:1px solid rgba(224,92,92,0.2)}
@keyframes spin{to{transform:rotate(360deg)}}
.sai-field{display:flex;flex-direction:column;gap:7px}
.sai-field label{font-size:13px;font-weight:600;color:rgba(255,255,255,.7);letter-spacing:.03em;text-transform:uppercase}
.sai-field input,.sai-field textarea{
  background:#ffffff !important;-webkit-appearance:none;
  -webkit-text-fill-color:#111 !important;color:#111 !important;
  border:1.5px solid rgba(109,92,230,.25);border-radius:10px;
  padding:12px 16px;font-size:15px;font-family:inherit;
  outline:none;transition:border-color .2s,box-shadow .2s;width:100%;box-sizing:border-box;
}
.sai-field input:focus,.sai-field textarea:focus{border-color:#6d5ce6;box-shadow:0 0 0 3px rgba(109,92,230,.15)}
.sai-field textarea{resize:vertical;min-height:120px}
.sai-form-row-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.sai-form-submit{display:flex;align-items:center;justify-content:center;gap:10px;background:linear-gradient(135deg,#6d5ce6,#0fa876);color:#fff;border:none;border-radius:12px;padding:16px 24px;font-size:16px;font-weight:700;font-family:inherit;cursor:pointer;width:100%;transition:opacity .2s,transform .1s}
.sai-form-submit:hover{opacity:.9;transform:translateY(-1px)}
.sai-form-submit:active{transform:translateY(0)}
@media(max-width:768px){
  .sai-cta-inner{grid-template-columns:1fr;gap:48px;text-align:center}
  .sai-cta-detail{justify-content:center}
  .sai-form-row-2{grid-template-columns:1fr}
  .sai-form-card{padding:24px 20px}
}

/* FOOTER */
.sai-footer{
  border-top:1px solid var(--border);padding:48px 48px 40px;
  display:flex;justify-content:space-between;align-items:center;
  font-size:13px;color:var(--txt3);background:#fff;
}
.sai-footer a{color:var(--txt3);text-decoration:none;transition:color .2s}
.sai-footer a:hover{color:var(--txt)}
.sai-footer-links{display:flex;gap:28px}

/* Responsive */
/* HAMBURGER */
.sai-hamburger{
  display:none;flex-direction:column;justify-content:center;align-items:center;
  width:40px;height:40px;cursor:pointer;gap:5px;padding:8px;
  border:none;background:transparent;border-radius:8px;flex-shrink:0;
}
.sai-hamburger span{
  display:block;width:22px;height:2px;background:var(--txt);border-radius:2px;
  transition:transform .28s ease,opacity .2s ease;
}
.sai-hamburger.open span:nth-child(1){transform:translateY(7px) rotate(45deg)}
.sai-hamburger.open span:nth-child(2){opacity:0;transform:scaleX(0)}
.sai-hamburger.open span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}

/* MOBILE MENU PANEL */
.sai-mobile-menu{
  position:fixed;top:64px;left:0;right:0;z-index:98;
  background:rgba(255,255,255,0.97);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  display:flex;flex-direction:column;gap:0;
  transform:translateY(-8px);opacity:0;pointer-events:none;
  transition:transform .28s ease,opacity .25s ease;
}
.sai-mobile-menu.open{transform:translateY(0);opacity:1;pointer-events:all}
.sai-mobile-menu a{
  display:block;padding:15px 24px;font-size:16px;font-weight:500;
  color:var(--txt2);text-decoration:none;border-bottom:1px solid var(--border);
  transition:color .15s,background .15s;
}
.sai-mobile-menu a:hover{color:var(--txt);background:var(--bg2)}
.sai-mobile-menu a:last-child{border-bottom:none}
.sai-mobile-login-link{
  margin:12px 24px 20px !important;
  background:var(--accent) !important;color:#fff !important;
  border-radius:8px;border-bottom:none !important;
  padding:13px 24px !important;text-align:center;font-weight:700 !important;
  display:flex !important;align-items:center;justify-content:center;gap:8px;
}
.sai-mobile-login-link:hover{opacity:.88;background:var(--accent) !important}

@media(max-width:900px){
  .sai-nav{padding:0 20px}
  .sai-nav-links{display:none}
  .sai-nav-cta{display:none}
  .sai-hamburger{display:flex}
  .sai-mockup-body{grid-template-columns:1fr;min-height:auto}
  .sai-mock-sidebar,.sai-mock-panel{display:none}
  .sai-stats-strip{grid-template-columns:1fr 1fr}
  .sai-stat:nth-child(2){border-right:none}
  .sai-pipeline-grid{grid-template-columns:1fr}
  .sai-features-grid{grid-template-columns:1fr 1fr}
  .sai-compare-grid{grid-template-columns:1fr}
  .sai-integ-grid{grid-template-columns:1fr}
  .sai-integ-grid.reverse{direction:ltr}
  .sai-pricing-grid{grid-template-columns:1fr;max-width:460px}
  .sai-pricing-card.featured{transform:none}
}
@media(max-width:600px){
  .sai-features-grid{grid-template-columns:1fr}
  .sai-stats-strip{grid-template-columns:1fr 1fr}
  .sai-footer{flex-direction:column;gap:20px;text-align:center;padding:36px 20px 28px}
  .sai-footer-links{flex-wrap:wrap;justify-content:center;gap:8px 16px}
}
@media(max-width:480px){
  .sai-hero-badge{flex-wrap:wrap;justify-content:center;text-align:center;max-width:260px;padding:8px 18px;line-height:1.6;border-radius:20px}
  .sai-badge-part1{white-space:nowrap}
  .sai-badge-part2{white-space:nowrap}
}

/* HERO ANIMATION */
.sai-hidden{display:none!important}
@keyframes heroSlideIn{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:none}}
.hero-line-anim{animation:heroSlideIn .35s ease both}
@keyframes heroPopIn{from{opacity:0;transform:scale(.92) translateY(4px)}to{opacity:1;transform:none}}
.hero-msg-pop{animation:heroPopIn .3s ease both}
@keyframes heroDotFlash{0%,80%,100%{opacity:.2}40%{opacity:1}}
.hero-streaming-dots span{display:inline-block;width:4px;height:4px;border-radius:50%;background:var(--e-txt2);margin:0 1px;animation:heroDotFlash 1.2s infinite}
.hero-streaming-dots span:nth-child(2){animation-delay:.2s}
.hero-streaming-dots span:nth-child(3){animation-delay:.4s}
.hero-send-btn{width:28px;height:28px;border-radius:6px;background:var(--accent);border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;opacity:0;transition:opacity .3s;padding:0}
.hero-send-btn.visible{opacity:1}
.hero-send-btn svg{width:13px;height:13px;fill:none;stroke:#fff;stroke-width:2.5}
@media(max-width:900px){
  .sai-hero-mockup{max-width:calc(100vw - 48px)}
}
`;

export function LandingPage() {
  useEffect(() => {
    const s = document.createElement('style');
    s.id = 'sai-landing-css';
    s.textContent = CSS;
    document.head.appendChild(s);
    const prevOverflow = document.body.style.overflow;
    const prevBg = document.body.style.background;
    document.body.style.overflow = 'auto';
    document.body.style.background = '#ffffff';
    return () => {
      s.remove();
      document.body.style.overflow = prevOverflow;
      document.body.style.background = prevBg;
    };
  }, []);


  // Hero mockup animation
  useEffect(() => {
    let active = true;
    const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms));
    const g = (id: string) => document.getElementById(id);

    function showEl(id: string) { const el = g(id); if (el) el.classList.remove('sai-hidden'); }
    function hideEl(id: string) { const el = g(id); if (el) el.classList.add('sai-hidden'); }
    function animLine(id: string) {
      const el = g(id); if (!el) return;
      el.classList.remove('sai-hidden'); el.classList.remove('hero-line-anim');
      void (el as HTMLElement).offsetWidth;
      el.classList.add('hero-line-anim');
    }
    function addMsg(html: string, extraClass = '') {
      const area = g('heroMessages'); if (!area) return null;
      const div = document.createElement('div');
      div.className = 'sai-msg hero-msg-pop ' + extraClass;
      div.innerHTML = html; area.appendChild(div); return div;
    }
    function addStep(icon: string, label: string, text: string, color = '#34d399') {
      const area = g('heroMessages'); if (!area) return null;
      const div = document.createElement('div');
      div.className = 'sai-msg-step hero-msg-pop';
      div.innerHTML = `<span class="sai-step-icon">${icon}</span><div><span class="sai-step-label" style="color:${color}">${label}</span><span class="sai-step-text">${text}</span></div>`;
      area.appendChild(div); return div;
    }
    async function streamText(el: HTMLElement, text: string, delay = 55) {
      const words = text.split(' '); el.textContent = '';
      for (const w of words) {
        if (!active) return;
        el.textContent += (el.textContent ? ' ' : '') + w;
        await sleep(delay);
      }
    }
    async function typeInput(text: string, delay = 60) {
      const inp = g('heroChatInput'); if (!inp) return;
      inp.textContent = ''; (inp as HTMLElement).style.color = 'var(--e-txt)';
      for (const ch of text) {
        if (!active) return;
        inp.textContent += ch; await sleep(delay);
      }
      const btn = g('heroSendBtn'); if (btn) btn.classList.add('visible');
    }
    function clearInput() {
      const inp = g('heroChatInput');
      if (inp) { inp.textContent = 'Ask SurgicalAI…'; (inp as HTMLElement).style.color = ''; }
      const btn = g('heroSendBtn'); if (btn) btn.classList.remove('visible');
    }
    function resetCode() {
      ['heroDel50','heroDel51','heroDel52','heroAdd50','heroAdd51','heroAdd52','heroCl57qa'].forEach(id => {
        const el = g(id); if (el) { el.classList.add('sai-hidden'); el.classList.remove('hero-line-anim'); }
      });
      ['heroCl53','heroCl54','heroCl55'].forEach(id => { const el = g(id); if (el) el.classList.remove('sai-hidden'); });
      const badge = g('heroPanelBadge');
      if (badge) { badge.textContent = 'Architect'; badge.className = 'sai-panel-badge sai-badge-arch'; (badge as HTMLElement).style.cssText = ''; }
      const title = g('heroTitlebarText');
      if (title) title.textContent = 'SurgicalAI — ChatPanel.tsx';
    }

    async function runLoop() {
      while (active) {
        resetCode();
        const area = g('heroMessages'); if (area) area.innerHTML = '';
        clearInput();
        await sleep(1400); if (!active) return;

        await typeInput('Add streaming to handleSend and wire it to the Surgical editor', 45);
        if (!active) return; await sleep(500); if (!active) return;
        const sendBtn = g('heroSendBtn'); if (sendBtn) sendBtn.classList.remove('visible');
        addMsg('Add streaming to handleSend and wire it to the Surgical editor', 'sai-msg-user');
        clearInput(); await sleep(600); if (!active) return;

        const archDiv = addMsg('', 'sai-msg-arch');
        if (archDiv) {
          archDiv.innerHTML = '<strong style="color:#e0e0f0;font-size:11px;display:block;margin-bottom:4px">🏛 Architect · Plan</strong><span id="heroArchStream"></span>';
          const archSpan = g('heroArchStream');
          if (archSpan) {
            archSpan.innerHTML = '<span class="hero-streaming-dots"><span></span><span></span><span></span></span>';
            await sleep(900); if (!active) return;
            await streamText(archSpan as HTMLElement, 'Small targeted change in ChatPanel.tsx line 50–52. Routing → Surgeon path (focused window extracted).', 48);
          }
        }
        await sleep(500); if (!active) return;

        const badge = g('heroPanelBadge');
        if (badge) { badge.textContent = 'Surgeon'; badge.className = 'sai-panel-badge sai-badge-surg'; (badge as HTMLElement).style.cssText = ''; }
        const title = g('heroTitlebarText');
        if (title) title.textContent = 'SurgicalAI — ChatPanel.tsx · editing…';
        const surgStep = addStep('⚡', 'Surgeon · SEARCH/REPLACE', 'Applying…', '#34d399');
        await sleep(400); if (!active) return;

        hideEl('heroCl53'); hideEl('heroCl54'); hideEl('heroCl55');
        await sleep(200); if (!active) return;
        animLine('heroDel50'); await sleep(180); if (!active) return;
        animLine('heroDel51'); await sleep(180); if (!active) return;
        animLine('heroDel52'); await sleep(350); if (!active) return;
        hideEl('heroDel50'); hideEl('heroDel51'); hideEl('heroDel52');
        animLine('heroAdd50'); await sleep(180); if (!active) return;
        animLine('heroAdd51'); await sleep(180); if (!active) return;
        animLine('heroAdd52'); await sleep(400); if (!active) return;
        showEl('heroCl53'); showEl('heroCl54'); showEl('heroCl55');
        if (surgStep) {
          const st = surgStep.querySelector('.sai-step-text');
          if (st) st.textContent = '3 lines replaced · ChatPanel.tsx · confidence 98%';
        }
        await sleep(700); if (!active) return;

        if (badge) { badge.textContent = 'QA'; badge.className = 'sai-panel-badge'; (badge as HTMLElement).style.cssText = 'background:rgba(217,119,6,.2);color:#fbbf24'; }
        const qaStep = addStep('⏳', 'QA · TypeScript', 'Compiling…', '#fbbf24');
        await sleep(1400); if (!active) return;
        if (qaStep) {
          const qi = qaStep.querySelector('.sai-step-icon'); if (qi) qi.textContent = '✅';
          const ql = qaStep.querySelector('.sai-step-label') as HTMLElement | null;
          if (ql) { ql.style.color = '#34d399'; ql.textContent = 'QA · TypeScript'; }
          const qt = qaStep.querySelector('.sai-step-text'); if (qt) qt.textContent = '0 errors · build clean · applied to file';
          (qaStep as HTMLElement).style.borderColor = 'rgba(15,168,118,.4)';
        }
        if (badge) { badge.textContent = 'Done'; (badge as HTMLElement).style.cssText = 'background:rgba(15,168,118,.2);color:#34d399'; }
        animLine('heroCl57qa');
        await sleep(3200); if (!active) return;
      }
    }

    runLoop();
    return () => { active = false; };
  }, []);

  // QA terminal animation
  useEffect(() => {
    const termOrNull = document.getElementById('qaTerm');
    if (!termOrNull) return;
    const term: HTMLElement = termOrNull;
    const stepEls = ['qaStep1','qaStep2','qaStep3','qaStep4'].map(id => document.getElementById(id));
    let cursor: HTMLElement | null = null;
    let fired = false;

    function setCursor(el: HTMLElement) {
      if (cursor && cursor.parentNode) cursor.parentNode.removeChild(cursor);
      cursor = document.createElement('span');
      cursor.className = 'qa-cursor';
      el.appendChild(cursor);
    }
    function removeCursor() {
      if (cursor && cursor.parentNode) cursor.parentNode.removeChild(cursor);
      cursor = null;
    }
    function scroll() { term.scrollTop = term.scrollHeight; }
    function instant(cls: string, html?: string): HTMLElement {
      const el = document.createElement('span');
      el.className = 'sai-tl ' + cls;
      if (html) el.innerHTML = html;
      term.appendChild(el);
      scroll();
      return el;
    }
    function typeIn(cls: string, text: string, speed: number, next: () => void) {
      removeCursor();
      const el = document.createElement('span');
      el.className = 'sai-tl ' + cls;
      term.appendChild(el);
      setCursor(el);
      let i = 0;
      function step() {
        if (i < text.length) {
          el.insertBefore(document.createTextNode(text[i++]), cursor);
          scroll();
          setTimeout(step, speed + Math.random() * speed * 0.35);
        } else { removeCursor(); next(); }
      }
      step();
    }
    function showDots(label: string, duration: number, next: () => void) {
      removeCursor();
      const el = document.createElement('span');
      el.className = 'sai-tl dim';
      term.appendChild(el);
      setCursor(el);
      let i = 0;
      const states = [label + ' .', label + ' ..', label + ' ...'];
      const iv = setInterval(() => { el.textContent = states[i++ % 3]; setCursor(el); scroll(); }, 320);
      setTimeout(() => { clearInterval(iv); if (el.parentNode) el.parentNode.removeChild(el); removeCursor(); next(); }, duration);
    }
    function activateStep(idx: number) {
      stepEls.forEach(s => s && s.classList.remove('qa-step-active'));
      if (stepEls[idx]) stepEls[idx]!.classList.add('qa-step-active');
    }
    function doneStep(idx: number) {
      if (stepEls[idx]) { stepEls[idx]!.classList.remove('qa-step-active'); stepEls[idx]!.classList.add('qa-step-done'); }
    }
    function wait(ms: number, fn: () => void) { setTimeout(fn, ms); }

    function run() {
      activateStep(0);
      typeIn('dim', '▶ Surgical edit applied — 3 lines changed in Dashboard.tsx', 20, () => {
        doneStep(0); instant('blank');
        wait(350, () => {
          activateStep(1);
          wait(150, () => {
            typeIn('cmd', '$ tsc --noEmit --strict', 32, () => {
              wait(180, () => {
                showDots('Compiling TypeScript', 1500, () => {
                  instant('err', 'src/components/Dashboard.tsx(142,18): error TS2345:');
                  wait(140, () => {
                    instant('err', '\u00a0\u00a0Argument of type \u2018string | undefined\u2019 is not');
                    wait(110, () => {
                      instant('err', '\u00a0\u00a0assignable to parameter of type \u2018string\u2019.');
                      wait(220, () => {
                        instant('err', 'src/components/Dashboard.tsx(156,9): error TS2322:');
                        wait(140, () => {
                          instant('err', '\u00a0\u00a0Type \u2018number\u2019 is not assignable to type \u2018string\u2019.');
                          wait(320, () => {
                            const warn = instant('warn', 'Found 2 errors.\u00a0\u00a0');
                            const badge = document.createElement('span');
                            badge.className = 'sai-score-fail';
                            badge.textContent = '❌ QA Score: 2 / 10';
                            warn.appendChild(badge); scroll();
                            doneStep(1);
                            wait(550, () => {
                              instant('blank'); instant('divider');
                              wait(380, () => {
                                activateStep(2);
                                wait(120, () => {
                                  instant('info', '↻ Auto-heal attempt 1 / 3');
                                  wait(280, () => {
                                    typeIn('dim', '  Sending 2 errors + full file to Claude Surgeon...', 17, () => {
                                      wait(380, () => {
                                        typeIn('dim', '  Claude: root cause → null guard + implicit coercion', 17, () => {
                                          wait(280, () => {
                                            instant('dim', '  Patch applied. Re-running tsc...');
                                            instant('blank');
                                            doneStep(2);
                                            wait(550, () => {
                                              activateStep(3);
                                              wait(120, () => {
                                                typeIn('cmd', '$ tsc --noEmit --strict', 32, () => {
                                                  wait(180, () => {
                                                    showDots('Compiling TypeScript', 1100, () => {
                                                      instant('ok', '✓\u00a0\u00a00 errors\u00a0\u00a0·\u00a0\u00a00 warnings');
                                                      wait(420, () => {
                                                        const passLine = instant('ok', '');
                                                        const badge2 = document.createElement('span');
                                                        badge2.className = 'sai-score-pass';
                                                        badge2.textContent = '✦ QA PASS · Score: 9.4 / 10 · Attempt 1 of 3';
                                                        passLine.appendChild(badge2); scroll();
                                                        doneStep(3);
                                                      });
                                                    });
                                                  });
                                                });
                                              });
                                            });
                                          });
                                        });
                                      });
                                    });
                                  });
                                });
                              });
                            });
                          });
                        });
                      });
                    });
                  });
                });
              });
            });
          });
        });
      });
    }

    const obs = new IntersectionObserver((entries) => {
      entries.forEach(e => { if (e.isIntersecting && !fired) { fired = true; obs.disconnect(); run(); } });
    }, { threshold: 0.25 });
    obs.observe(term);
    return () => obs.disconnect();
  }, []);

  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [formSuccess, setFormSuccess] = useState(false);
  const [formSubmitting, setFormSubmitting] = useState(false);
  const [formError, setFormError] = useState('');

  async function handleContactSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    setFormSubmitting(true);
    setFormError('');
    try {
      const data = new FormData(form);
      const res = await fetch('https://formspree.io/f/mzdwkojb', {
        method: 'POST',
        body: data,
        headers: { Accept: 'application/json' },
      });
      if (res.ok) {
        setFormSuccess(true);
        form.reset();
      } else {
        const json = await res.json().catch(() => ({}));
        setFormError((json as any)?.error || 'Something went wrong. Please try again.');
      }
    } catch {
      setFormError('Network error. Please check your connection and try again.');
    } finally {
      setFormSubmitting(false);
    }
  }
  const closeMobileMenu = () => setMobileMenuOpen(false);

  return (
    <>
      {/* NAV */}
      <nav className="sai-nav">
        <a href="#" className="sai-nav-logo">
          <svg viewBox="0 0 28 28" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect width="28" height="28" rx="8" fill="url(#sai-g1)"/>
            <path d="M8 14 L13 9 L13 19" stroke="white" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/>
            <path d="M15 9 L20 14 L15 19" stroke="rgba(255,255,255,0.5)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/>
            <defs>
              <linearGradient id="sai-g1" x1="0" y1="0" x2="28" y2="28" gradientUnits="userSpaceOnUse">
                <stop stopColor="#6d5ce6"/><stop offset="1" stopColor="#0fa876"/>
              </linearGradient>
            </defs>
          </svg>
          SurgicalAI
        </a>
        <ul className="sai-nav-links">
          <li><a href="#how-it-works">How it works</a></li>
          <li><a href="#features">Features</a></li>
          <li><a href="#integrations">Integrations</a></li>
          <li><a href="#pricing">Pricing</a></li>
          <li><a href="#compare">Compare</a></li>
        </ul>
        <button
          className={`sai-hamburger${mobileMenuOpen ? ' open' : ''}`}
          onClick={() => setMobileMenuOpen(v => !v)}
          aria-label="Toggle menu"
        >
          <span/><span/><span/>
        </button>
        <a href="/login" className="sai-nav-cta">
          <LoginIcon style={{ fontSize: 16 }} />
          Login
        </a>
      </nav>

      {/* MOBILE MENU */}
      <div className={`sai-mobile-menu${mobileMenuOpen ? ' open' : ''}`}>
        <a href="#how-it-works" onClick={closeMobileMenu}>How it works</a>
        <a href="#features" onClick={closeMobileMenu}>Features</a>
        <a href="#integrations" onClick={closeMobileMenu}>Integrations</a>
        <a href="#pricing" onClick={closeMobileMenu}>Pricing</a>
        <a href="#compare" onClick={closeMobileMenu}>Compare</a>
        <a href="/login" className="sai-mobile-login-link">
          <LoginIcon style={{ fontSize: 16 }} />
          Login to SurgicalAI
        </a>
      </div>

      {/* HERO */}
      <section className="sai-hero">
        <div className="sai-hero-glow"></div>
        <div className="sai-hero-badge">
          <span className="sai-hero-badge-dot"></span>
          <span className="sai-badge-part1">Powered by Claude&nbsp;·&nbsp;</span>
          <span className="sai-badge-part2">Architect&nbsp;+&nbsp;Surgeon</span>
        </div>
        <h1 className="sai-h1">Code edits with<br/><span>surgical precision</span></h1>
        <p className="sai-hero-sub">SurgicalAI sends an Architect to plan, a Surgeon to operate, and a QA agent to verify. Zero guessing. Zero silent failures.</p>
        <div className="sai-hero-actions">
          <a href="/login" className="sai-btn-primary">
            <LoginIcon style={{ fontSize: 16 }} />
            Login to SurgicalAI
          </a>
          <a href="#how-it-works" className="sai-btn-secondary">See how it works →</a>
        </div>

        {/* APP MOCKUP — animated */}
        <div className="sai-hero-mockup">
          <div className="sai-mockup-titlebar">
            <div className="sai-dot sai-dot-r"></div>
            <div className="sai-dot sai-dot-y"></div>
            <div className="sai-dot sai-dot-g"></div>
            <span className="sai-titlebar-text" id="heroTitlebarText">SurgicalAI — ChatPanel.tsx</span>
          </div>
          <div className="sai-mockup-body">
            {/* Sidebar */}
            <div className="sai-mock-sidebar">
              <div className="sai-sidebar-section">Project</div>
              <div className="sai-mock-file active">
                <svg className="sai-file-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/>
                </svg>
                ChatPanel.tsx
              </div>
              <div className="sai-mock-file">
                <svg className="sai-file-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/>
                </svg>
                pipeline.py
              </div>
              <div className="sai-mock-file">
                <svg className="sai-file-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/>
                </svg>
                surgical_editor.py
              </div>
              <div className="sai-mock-file">
                <svg className="sai-file-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/>
                </svg>
                linter_validator.py
              </div>
              <div style={{marginTop:'16px'}}>
                <div className="sai-sidebar-section">Git</div>
                <div className="sai-mock-file"><span style={{fontSize:'10px',color:'#34d399',marginRight:'4px'}}>●</span>fix/surgeon-patch</div>
                <div className="sai-mock-file" style={{color:'#404060'}}><span style={{fontSize:'10px',marginRight:'4px'}}>○</span>main</div>
              </div>
            </div>

            {/* Code Editor */}
            <div className="sai-mock-editor">
              <div className="sai-editor-tabs">
                <div className="sai-mock-tab active">ChatPanel.tsx</div>
                <div className="sai-mock-tab">pipeline.py</div>
              </div>
              <div className="sai-mock-code">
                <div className="sai-line"><span className="sai-ln">47</span><span className="sai-ct"><span className="sai-kw">const</span> <span className="sai-fn">handleSend</span> = <span className="sai-kw">async</span> () =&gt; {'{'}</span></div>
                <div className="sai-line"><span className="sai-ln">48</span><span className="sai-ct">  <span className="sai-kw">if</span> (!input.trim()) <span className="sai-kw">return</span>;</span></div>
                <div className="sai-line"><span className="sai-ln">49</span><span className="sai-ct">  <span className="sai-fn">setIsLoading</span>(<span className="sai-kw">true</span>);</span></div>
                <div className="sai-line del sai-hidden" id="heroDel50"><span className="sai-ln">50</span><span className="sai-ct"><span className="sai-op">-</span>  <span className="sai-kw">const</span> res = <span className="sai-kw">await</span> <span className="sai-fn">fetch</span>(<span className="sai-str">'/api/chat'</span>, {'{'}</span></div>
                <div className="sai-line del sai-hidden" id="heroDel51"><span className="sai-ln">51</span><span className="sai-ct"><span className="sai-op">-</span>    method: <span className="sai-str">'POST'</span>, body: input</span></div>
                <div className="sai-line del sai-hidden" id="heroDel52"><span className="sai-ln">52</span><span className="sai-ct"><span className="sai-op">-</span>  {'}'});</span></div>
                <div className="sai-line add sai-hidden" id="heroAdd50"><span className="sai-ln">50</span><span className="sai-ct"><span className="sai-op">+</span>  <span className="sai-kw">const</span> res = <span className="sai-kw">await</span> <span className="sai-fn">streamSurgicalEdit</span>({'{'}</span></div>
                <div className="sai-line add sai-hidden" id="heroAdd51"><span className="sai-ln">51</span><span className="sai-ct"><span className="sai-op">+</span>    prompt: input, fileIds: sessionFiles</span></div>
                <div className="sai-line add sai-hidden" id="heroAdd52"><span className="sai-ln">52</span><span className="sai-ct"><span className="sai-op">+</span>  {'}'});</span></div>
                <div className="sai-line" id="heroCl53"><span className="sai-ln">53</span><span className="sai-ct">  <span className="sai-kw">const</span> data = <span className="sai-kw">await</span> res.<span className="sai-fn">json</span>();</span></div>
                <div className="sai-line" id="heroCl54"><span className="sai-ln">54</span><span className="sai-ct">  <span className="sai-fn">setMessages</span>(prev =&gt; [...prev, data]);</span></div>
                <div className="sai-line" id="heroCl55"><span className="sai-ln">55</span><span className="sai-ct">{'}'};</span></div>
                <div className="sai-line sai-hidden" id="heroCl57qa"><span className="sai-ln">57</span><span className="sai-ct"><span className="sai-cm">{'// Surgeon applied 3 lines · QA passed ✓'}</span></span></div>
                <div className="sai-line"><span className="sai-ln">58</span><span className="sai-ct"><span className="sai-kw">return</span> (</span></div>
                <div className="sai-line"><span className="sai-ln">59</span><span className="sai-ct">  &lt;<span className="sai-ty">ChatContainer</span>&gt;</span></div>
                <div className="sai-line"><span className="sai-ln">60</span><span className="sai-ct">    &lt;<span className="sai-ty">MessageList</span> messages={'{messages}'}/&gt;<span className="sai-cursor-blink"></span></span></div>
              </div>
            </div>

            {/* Chat/Pipeline Panel */}
            <div className="sai-mock-panel">
              <div className="sai-panel-header">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M4.22 4.22l2.83 2.83M16.95 16.95l2.83 2.83M1 12h4M19 12h4M4.22 19.78l2.83-2.83M16.95 7.05l2.83-2.83"/>
                </svg>
                Pipeline
                <span className="sai-panel-badge sai-badge-arch" id="heroPanelBadge">Architect</span>
              </div>
              <div className="sai-mock-messages" id="heroMessages"></div>
              <div className="sai-mock-input-bar">
                <div className="sai-mock-input" id="heroChatInput">Ask SurgicalAI…</div>
                <button className="hero-send-btn" id="heroSendBtn" aria-label="send">
                  <svg viewBox="0 0 24 24"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22,2 15,22 11,13 2,9"/></svg>
                </button>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* STATS */}
      <section style={{padding:'0 24px 80px'}}>
        <div className="sai-container">
          <div className="sai-stats-strip">
            <div className="sai-stat"><div className="sai-stat-num"><span>2</span></div><div className="sai-stat-label">AI Roles — Architect &amp; Surgeon</div></div>
            <div className="sai-stat"><div className="sai-stat-num"><span>0</span></div><div className="sai-stat-label">Silent failures — hard errors only</div></div>
            <div className="sai-stat"><div className="sai-stat-num">32<span>K</span></div><div className="sai-stat-label">Token ceiling for large rewrites</div></div>
            <div className="sai-stat"><div className="sai-stat-num"><span>3×</span></div><div className="sai-stat-label">Auto-heal retries on TS lint fail</div></div>
          </div>
        </div>
      </section>

      {/* HOW IT WORKS */}
      <section id="how-it-works" className="sai-section" style={{background:'var(--bg2)'}}>
        <div className="sai-container">
          <div className="sai-pipeline-grid">
            <div>
              <div className="sai-section-label">How it works</div>
              <h2 className="sai-section-title">Two agents.<br/>One source of truth.</h2>
              <p className="sai-section-sub" style={{marginBottom:'48px'}}>Every change passes through a strict four-stage pipeline. No shortcuts, no guessing.</p>
              <div className="sai-pipeline-steps">
                <div className="sai-pipe-step">
                  <div className="sai-pipe-step-num">1</div>
                  <div>
                    <h3>Architect plans the change</h3>
                    <p>Claude reads your request, identifies the exact symbol and line range, and decides whether to route to the Surgeon or trigger a full rewrite.</p>
                    <span className="sai-pipe-tag sai-tag-arch">Claude — Architect</span>
                  </div>
                </div>
                <div className="sai-pipe-step">
                  <div className="sai-pipe-step-num">2</div>
                  <div>
                    <h3>Size-based routing</h3>
                    <p>Small, focused changes (&lt;250 lines, 1 region) go to the Surgeon for precise SEARCH/REPLACE. Large multi-region changes go to direct rewrite at 32K tokens.</p>
                    <span className="sai-pipe-tag sai-tag-surg">Auto-routed</span>
                  </div>
                </div>
                <div className="sai-pipe-step">
                  <div className="sai-pipe-step-num">3</div>
                  <div>
                    <h3>Surgeon applies the operation</h3>
                    <p>The Surgeon operates on a focused window of the live file — never a stale copy. SEARCH strings are validated before any change is committed.</p>
                    <span className="sai-pipe-tag sai-tag-surg">Claude — Surgeon</span>
                  </div>
                </div>
                <div className="sai-pipe-step">
                  <div className="sai-pipe-step-num">4</div>
                  <div>
                    <h3>QA auto-heals on failure</h3>
                    <p>TypeScript is compiled after every change. Any error triggers up to 3 auto-heal attempts — Claude sees the fresh error output and the live file each time.</p>
                    <span className="sai-pipe-tag sai-tag-qa">QA · Auto-heal</span>
                  </div>
                </div>
              </div>
            </div>
            <div className="sai-pipeline-visual">
              <div className="sai-pipe-card">
                <div className="sai-pipe-card-label">Step 1 · Architect</div>
                <div className="sai-pipe-card-title">📋 Analyzing request…</div>
                <div className="sai-pipe-card-desc">Symbol: <code style={{color:'var(--accent)',fontSize:'12px'}}>handleSend()</code> · File: ChatPanel.tsx · Lines 50–55<br/>Decision: Surgeon path — focused window extracted</div>
              </div>
              <div className="sai-pipe-connector"></div>
              <div className="sai-pipe-card">
                <div className="sai-pipe-card-label">Step 2 · Routing</div>
                <div className="sai-pipe-card-title">⚡ Surgeon path selected</div>
                <div className="sai-pipe-card-desc">Region size: 6 lines · Threshold: 250 lines<br/>Focused window: lines 47–60 passed to Surgeon</div>
              </div>
              <div className="sai-pipe-connector"></div>
              <div className="sai-pipe-card">
                <div className="sai-pipe-card-label">Step 3 · Surgeon</div>
                <div className="sai-pipe-card-title">🔬 SEARCH/REPLACE applied</div>
                <div className="sai-pipe-card-desc">3 lines replaced · SEARCH string validated ✓<br/>File written to disk → session updated</div>
              </div>
              <div className="sai-pipe-connector"></div>
              <div className="sai-pipe-card" style={{borderColor:'rgba(15,168,118,.3)'}}>
                <div className="sai-pipe-card-label">Step 4 · QA</div>
                <div className="sai-pipe-card-title" style={{color:'var(--green)'}}>✅ TypeScript: 0 errors</div>
                <div className="sai-pipe-card-desc">Build clean · Change committed · Stream complete</div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* DIFF DEMO */}
      <section className="sai-section" id="features">
        <div className="sai-container">
          <div className="sai-section-label" style={{marginBottom:'16px'}}>Precision editing</div>
          <h2 className="sai-section-title" style={{marginBottom:'12px'}}>The Surgeon never guesses</h2>
          <p className="sai-section-sub" style={{marginBottom:'48px'}}>Every SEARCH string is extracted live from the file on disk. If it doesn't match exactly, the operation fails loudly — never silently.</p>
          <div className="sai-diff-section">
            <div className="sai-diff-header">
              <span className="sai-diff-filename">ChatPanel.tsx</span>
              <span style={{color:'#404060',fontSize:'12px'}}>lines 50–52</span>
              <span className="sai-diff-badge surg">Surgeon · SEARCH/REPLACE</span>
            </div>
            <div className="sai-diff-body">
              <div className="sai-diff-line"><span className="sai-diff-sign n">·</span><span className="sai-diff-code" style={{color:'#404060'}}>{'  if (!input.trim()) return;'}</span></div>
              <div className="sai-diff-line"><span className="sai-diff-sign n">·</span><span className="sai-diff-code" style={{color:'#404060'}}>{'  setIsLoading(true);'}</span></div>
              <div className="sai-diff-line minus"><span className="sai-diff-sign m">−</span><span className="sai-diff-code">{"  const res = await fetch('/api/chat', {"}</span></div>
              <div className="sai-diff-line minus"><span className="sai-diff-sign m">−</span><span className="sai-diff-code">{'    method: \'POST\', body: input'}</span></div>
              <div className="sai-diff-line minus"><span className="sai-diff-sign m">−</span><span className="sai-diff-code">{'  });'}</span></div>
              <div className="sai-diff-line plus"><span className="sai-diff-sign p">+</span><span className="sai-diff-code">{'  const res = await streamSurgicalEdit({'}</span></div>
              <div className="sai-diff-line plus"><span className="sai-diff-sign p">+</span><span className="sai-diff-code">{'    prompt: input, fileIds: sessionFiles'}</span></div>
              <div className="sai-diff-line plus"><span className="sai-diff-sign p">+</span><span className="sai-diff-code">{'  });'}</span></div>
              <div className="sai-diff-line"><span className="sai-diff-sign n">·</span><span className="sai-diff-code" style={{color:'#404060'}}>{'  const data = await res.json();'}</span></div>
            </div>
          </div>
        </div>
      </section>

      {/* FEATURES */}
      <section className="sai-section" style={{paddingTop:'0',background:'var(--bg2)'}}>
        <div className="sai-container">
          <div className="sai-section-label" style={{marginBottom:'16px'}}>Capabilities</div>
          <h2 className="sai-section-title" style={{marginBottom:'48px'}}>Built for real codebases</h2>
          <div className="sai-features-grid">
            <div className="sai-feat-card"><div className="sai-feat-icon">🏛</div><h3>Dual-role AI pipeline</h3><p>The Architect plans and routes. The Surgeon operates. Two models, two jobs, zero role confusion.</p></div>
            <div className="sai-feat-card"><div className="sai-feat-icon">🔬</div><h3>SEARCH/REPLACE precision</h3><p>Changes are applied by finding and replacing exact strings in a focused window — not overwriting entire files.</p></div>
            <div className="sai-feat-card"><div className="sai-feat-icon">⚡</div><h3>Size-based routing</h3><p>Small changes go to the Surgeon. Large multi-region rewrites go direct at 32K tokens. Routing is automatic.</p></div>
            <div className="sai-feat-card"><div className="sai-feat-icon">✅</div><h3>QA with auto-heal</h3><p>TypeScript is compiled after every change. Hard errors trigger up to 3 Claude-powered heal attempts automatically.</p></div>
            <div className="sai-feat-card"><div className="sai-feat-icon">🔁</div><h3>Retry &amp; backoff</h3><p>Anthropic 529 overload errors are retried with exponential backoff — 10s, 20s, 40s — before surfacing a failure.</p></div>
            <div className="sai-feat-card"><div className="sai-feat-icon">📡</div><h3>Real-time streaming</h3><p>Every pipeline stage — plan, route, operate, QA — streams live to the UI. You see the thinking as it happens.</p></div>
          </div>
        </div>
      </section>

      {/* QA SHOWCASE */}
      <section className="sai-qa-section" id="qa">
        <div className="sai-qa-wrap">
          <div className="sai-qa-head">
            <div className="sai-qa-pill"><span className="sai-qa-pill-dot"></span>Zero-Defect Engine</div>
            <h2 className="sai-qa-h2">QA that <span className="grad">actually bites</span></h2>
            <p className="sai-qa-sub">Every surgical edit is TypeScript-compiled, scored out of 10, and automatically healed by Claude — before a single line touches your repo.</p>
          </div>
          <div className="sai-qa-body">
            <div className="sai-qa-steps">
              <div className="sai-qa-step" id="qaStep1">
                <div className="sai-qa-step-num">01</div>
                <div className="sai-qa-step-text">
                  <strong>Surgeon applies the edit</strong>
                  <p>SEARCH/REPLACE lands on the exact region — no full-file overwrite. Focused window only.</p>
                </div>
              </div>
              <div className="sai-qa-connector"></div>
              <div className="sai-qa-step" id="qaStep2">
                <div className="sai-qa-step-num">02</div>
                <div className="sai-qa-step-text">
                  <strong>TypeScript audit fires immediately</strong>
                  <p>tsc runs with <code>--noEmit --strict</code>. Any <code>error TS####</code> line hard-fails the score to ≤ 3 — no exceptions.</p>
                </div>
              </div>
              <div className="sai-qa-connector"></div>
              <div className="sai-qa-step" id="qaStep3">
                <div className="sai-qa-step-num">03</div>
                <div className="sai-qa-step-text">
                  <strong>Auto-heal loop kicks in</strong>
                  <p>Claude receives every raw error + the full live file. Up to 3 retry attempts, each with a fresh tsc run.</p>
                </div>
              </div>
              <div className="sai-qa-connector"></div>
              <div className="sai-qa-step is-pass" id="qaStep4">
                <div className="sai-qa-step-num">✓</div>
                <div className="sai-qa-step-text">
                  <strong>Clean pass — or hard stop</strong>
                  <p>Zero errors → score 9+ → change committed. Still failing after 3 heals? Pipeline halts and tells you exactly why.</p>
                </div>
              </div>
            </div>
            <div className="sai-qa-terminal" id="qaTerminal">
              <div className="sai-term-bar">
                <span className="sai-term-btn r"></span>
                <span className="sai-term-btn y"></span>
                <span className="sai-term-btn g"></span>
                <span className="sai-term-title">surgicalai — QA Audit · Dashboard.tsx</span>
              </div>
              <div className="sai-term-body" id="qaTerm"></div>
            </div>
          </div>
          <div className="sai-qa-stats">
            <div className="sai-qa-stat"><span className="sai-qa-stat-n">3×</span><span className="sai-qa-stat-l">Auto-heal retries<br/>per change</span></div>
            <div className="sai-qa-stat"><span className="sai-qa-stat-n">0</span><span className="sai-qa-stat-l">Silent failures —<br/>every error surfaces</span></div>
            <div className="sai-qa-stat"><span className="sai-qa-stat-n">≤3</span><span className="sai-qa-stat-l">Hard-fail score on<br/>any TS error</span></div>
            <div className="sai-qa-stat"><span className="sai-qa-stat-n">10</span><span className="sai-qa-stat-l">Point scoring<br/>per change</span></div>
          </div>
        </div>
      </section>

      {/* GITHUB INTEGRATION */}
      <section id="integrations" className="sai-section">
        <div className="sai-container">
          <div className="sai-integ-grid">
            <div>
              <div className="sai-integ-logo-badge">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="var(--txt)"><path d="M12 2C6.477 2 2 6.477 2 12c0 4.418 2.865 8.167 6.839 9.49.5.092.682-.217.682-.482 0-.237-.009-.868-.013-1.703-2.782.604-3.369-1.342-3.369-1.342-.454-1.155-1.11-1.463-1.11-1.463-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.11-4.555-4.943 0-1.091.39-1.984 1.029-2.683-.103-.253-.446-1.27.098-2.647 0 0 .84-.269 2.75 1.026A9.578 9.578 0 0112 6.836a9.59 9.59 0 012.504.337c1.909-1.295 2.748-1.026 2.748-1.026.546 1.377.202 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.578.688.48C19.138 20.164 22 16.418 22 12c0-5.523-4.477-10-10-10z"/></svg>
                GitHub Integration
              </div>
              <div className="sai-section-label">Version Control</div>
              <h2 className="sai-section-title">Branch-aware.<br/>PR-ready.</h2>
              <p className="sai-section-sub">SurgicalAI knows your branch, your open PRs, and your commit history. Every edit is scoped to the right context — no cross-branch contamination.</p>
              <div className="sai-integ-bullets">
                <div className="sai-integ-bullet">
                  <div className="sai-integ-bullet-icon">🌿</div>
                  <div>
                    <h4>Branch-scoped edits</h4>
                    <p>The Architect reads your active branch before planning. Changes are always committed to the correct branch.</p>
                  </div>
                </div>
                <div className="sai-integ-bullet">
                  <div className="sai-integ-bullet-icon">🔀</div>
                  <div>
                    <h4>Auto PR creation</h4>
                    <p>After a successful surgical edit session, SurgicalAI can open a pull request with a descriptive title and diff summary.</p>
                  </div>
                </div>
                <div className="sai-integ-bullet">
                  <div className="sai-integ-bullet-icon">📜</div>
                  <div>
                    <h4>Commit history awareness</h4>
                    <p>The pipeline can inspect recent commits to understand intent and avoid re-introducing reverted code.</p>
                  </div>
                </div>
              </div>
            </div>
            <div>
              <div className="sai-integ-mockup">
                <div className="sai-integ-titlebar">
                  <div className="sai-dot sai-dot-r"></div>
                  <div className="sai-dot sai-dot-y"></div>
                  <div className="sai-dot sai-dot-g"></div>
                  <span className="sai-integ-tbtitle">surgicalai / Pull Requests</span>
                </div>
                <div className="sai-integ-body">
                  <div className="sai-integ-section-label">Open</div>
                  <div className="sai-integ-row active">
                    <div className="sai-integ-dot green"></div>
                    <span style={{flex:1}}>feat: streaming SEARCH/REPLACE surgeon path</span>
                    <span className="sai-integ-badge-sm open">Open</span>
                  </div>
                  <div className="sai-integ-row active">
                    <div className="sai-integ-dot green"></div>
                    <span style={{flex:1}}>fix: QA auto-heal 3 retry attempts</span>
                    <span className="sai-integ-badge-sm review">Review</span>
                  </div>
                  <div className="sai-integ-row">
                    <div className="sai-integ-dot green"></div>
                    <span style={{flex:1}}>fix: async direct rewrite AsyncAnthropic</span>
                    <span className="sai-integ-badge-sm open">Open</span>
                  </div>
                  <div className="sai-integ-section-divider"></div>
                  <div className="sai-integ-section-label">Merged</div>
                  <div className="sai-integ-row">
                    <div className="sai-integ-dot purple"></div>
                    <span style={{flex:1}}>fix: remove DELETE fast-path</span>
                    <span className="sai-integ-badge-sm merged">Merged</span>
                  </div>
                  <div className="sai-integ-row">
                    <div className="sai-integ-dot purple"></div>
                    <span style={{flex:1}}>feat: size-based routing threshold 250L</span>
                    <span className="sai-integ-badge-sm merged">Merged</span>
                  </div>
                  <div className="sai-integ-row">
                    <div className="sai-integ-dot purple"></div>
                    <span style={{flex:1}}>feat: Claude Surgeon model routing</span>
                    <span className="sai-integ-badge-sm merged">Merged</span>
                  </div>
                  <div className="sai-integ-section-divider"></div>
                  <div className="sai-integ-section-label">Active Branch</div>
                  <div className="sai-integ-row active">
                    <div className="sai-integ-dot green"></div>
                    <span style={{flex:1,fontFamily:'monospace',fontSize:'11px'}}>feat/landing-page</span>
                    <span className="sai-integ-meta">2 commits ahead</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* LINEAR INTEGRATION */}
      <section className="sai-section" style={{background:'var(--bg2)'}}>
        <div className="sai-container">
          <div className="sai-integ-grid reverse">
            <div>
              <div className="sai-integ-logo-badge">
                <svg width="20" height="20" viewBox="0 0 100 100" fill="none"><circle cx="50" cy="50" r="50" fill="#5E6AD2"/><path d="M17.55 64.64L35.36 82.45a34.43 34.43 0 01-17.81-17.81zm-3.36-8.97l30.14 30.14a34.5 34.5 0 01-9.99-3.35L17.9 64.12a34.49 34.49 0 01-3.71-8.45zM82.45 35.36L64.64 17.55a34.43 34.43 0 0117.81 17.81zm3.36 8.97L55.67 14.19a34.5 34.5 0 019.99 3.35l18.44 18.44a34.49 34.49 0 013.71 8.45zM80.52 75.17L24.83 19.48A34.5 34.5 0 0150 15.5c18.78 0 34 15.22 34 34a34.4 34.4 0 01-3.48 15.67zm-5.69 5.35A34.5 34.5 0 0150 84.5c-18.78 0-34-15.22-34-34a34.4 34.4 0 013.48-15.67L74.83 80.52z" fill="white"/></svg>
                Linear Integration
              </div>
              <div className="sai-section-label">Issue Tracking</div>
              <h2 className="sai-section-title">Issues linked.<br/>Progress tracked.</h2>
              <p className="sai-section-sub">SurgicalAI connects code changes directly to Linear issues. When the Surgeon applies a fix, the linked ticket moves. No manual status updates.</p>
              <div className="sai-integ-bullets">
                <div className="sai-integ-bullet">
                  <div className="sai-integ-bullet-icon">🎫</div>
                  <div>
                    <h4>Issue-linked edits</h4>
                    <p>Mention a Linear ticket in your prompt and SurgicalAI attaches the code change to that issue automatically.</p>
                  </div>
                </div>
                <div className="sai-integ-bullet">
                  <div className="sai-integ-bullet-icon">🔄</div>
                  <div>
                    <h4>Auto status updates</h4>
                    <p>When QA passes and a change is committed, the linked Linear issue moves from In Progress → In Review — no click required.</p>
                  </div>
                </div>
                <div className="sai-integ-bullet">
                  <div className="sai-integ-bullet-icon">🗂</div>
                  <div>
                    <h4>Ticket-to-code tracing</h4>
                    <p>Every surgical operation is logged against the ticket. Reviewers see exactly which lines changed and why.</p>
                  </div>
                </div>
              </div>
            </div>
            <div>
              <div className="sai-integ-mockup">
                <div className="sai-integ-titlebar">
                  <div className="sai-dot sai-dot-r"></div>
                  <div className="sai-dot sai-dot-y"></div>
                  <div className="sai-dot sai-dot-g"></div>
                  <span className="sai-integ-tbtitle">Linear — SurgicalAI Sprint</span>
                </div>
                <div className="sai-integ-body">
                  <div className="sai-integ-section-label">In Progress</div>
                  <div className="sai-integ-row active">
                    <div className="sai-integ-dot blue"></div>
                    <span style={{flex:1}}>SAI-42 · QA TS hard-fail + auto-heal</span>
                    <span className="sai-integ-badge-sm inprog">In Progress</span>
                  </div>
                  <div className="sai-integ-row active">
                    <div className="sai-integ-dot blue"></div>
                    <span style={{flex:1}}>SAI-43 · Surgeon SEARCH mismatch ValueError</span>
                    <span className="sai-integ-badge-sm inprog">In Progress</span>
                  </div>
                  <div className="sai-integ-section-divider"></div>
                  <div className="sai-integ-section-label">In Review</div>
                  <div className="sai-integ-row">
                    <div className="sai-integ-dot amber"></div>
                    <span style={{flex:1}}>SAI-38 · AsyncAnthropic 32K stream fix</span>
                    <span className="sai-integ-badge-sm review">In Review</span>
                  </div>
                  <div className="sai-integ-row">
                    <div className="sai-integ-dot amber"></div>
                    <span style={{flex:1}}>SAI-35 · Routing threshold 250 lines</span>
                    <span className="sai-integ-badge-sm review">In Review</span>
                  </div>
                  <div className="sai-integ-section-divider"></div>
                  <div className="sai-integ-section-label">Done</div>
                  <div className="sai-integ-row">
                    <div className="sai-integ-dot green"></div>
                    <span style={{flex:1}}>SAI-32 · Claude Surgeon model routing</span>
                    <span className="sai-integ-badge-sm done">Done</span>
                  </div>
                  <div className="sai-integ-row">
                    <div className="sai-integ-dot green"></div>
                    <span style={{flex:1}}>SAI-28 · Remove DELETE fast-path</span>
                    <span className="sai-integ-badge-sm done">Done</span>
                  </div>
                  <div className="sai-integ-row">
                    <div className="sai-integ-dot gray"></div>
                    <span style={{flex:1}}>SAI-27 · MermaidDiagram removal</span>
                    <span className="sai-integ-badge-sm done">Done</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* COMPARE */}
      <section id="compare" className="sai-section">
        <div className="sai-container">
          <div className="sai-section-label" style={{marginBottom:'16px'}}>vs The alternatives</div>
          <h2 className="sai-section-title" style={{marginBottom:'12px'}}>What makes this different</h2>
          <p className="sai-section-sub" style={{marginBottom:'48px'}}>Most AI editors rewrite files blindly. SurgicalAI treats every file like a patient — diagnosed first, operated on second, checked out third.</p>
          <div className="sai-compare-grid">
            <div className="sai-compare-card">
              <div className="sai-compare-header"><h3>Standard AI editors</h3><span className="sai-compare-logo">Generic</span></div>
              <div className="sai-compare-list">
                <div className="sai-compare-item"><span className="chk no">✗</span><p><strong>Silent rewrites</strong> — whole file replaced, no diff, no visibility</p></div>
                <div className="sai-compare-item"><span className="chk no">✗</span><p><strong>No QA step</strong> — TypeScript errors discovered at runtime</p></div>
                <div className="sai-compare-item"><span className="chk no">✗</span><p><strong>Single model</strong> — planning and editing collapsed into one role</p></div>
                <div className="sai-compare-item"><span className="chk no">✗</span><p><strong>No auto-heal</strong> — failed edits require manual retry</p></div>
                <div className="sai-compare-item"><span className="chk no">✗</span><p><strong>No integrations</strong> — no GitHub PR, no Linear ticket sync</p></div>
              </div>
            </div>
            <div className="sai-compare-card ours">
              <div className="sai-compare-header"><h3>SurgicalAI</h3><span className="sai-compare-logo ours">SurgicalAI</span></div>
              <div className="sai-compare-list">
                <div className="sai-compare-item"><span className="chk yes">✓</span><p><strong>SEARCH/REPLACE diffs</strong> — exact lines changed, visible in stream</p></div>
                <div className="sai-compare-item"><span className="chk yes">✓</span><p><strong>TypeScript QA on every change</strong> — hard fail, not silent pass</p></div>
                <div className="sai-compare-item"><span className="chk yes">✓</span><p><strong>Architect + Surgeon</strong> — dedicated roles, clear separation</p></div>
                <div className="sai-compare-item"><span className="chk yes">✓</span><p><strong>3× auto-heal</strong> — Claude re-reads errors and retries automatically</p></div>
                <div className="sai-compare-item"><span className="chk yes">✓</span><p><strong>GitHub + Linear</strong> — branch-aware, PR creation, ticket sync</p></div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* PRICING */}
      <section id="pricing" className="sai-section" style={{background:'var(--bg2)'}}>
        <div className="sai-container">
          <div style={{textAlign:'center',marginBottom:'64px'}}>
            <div className="sai-section-label">Pricing</div>
            <h2 className="sai-section-title">Simple, transparent pricing</h2>
            <p style={{fontSize:'16px',color:'var(--txt2)',maxWidth:'480px',margin:'0 auto',lineHeight:'1.7'}}>
              Two tiers aligned with Claude's model capabilities. Upgrade anytime — your files and sessions stay intact.
            </p>
          </div>
          {/* Stripe wiring: add onClick={() => stripeCheckout(priceId)} to buttons below */}
          <div className="sai-pricing-grid">

            {/* STARTER */}
            <div className="sai-pricing-card">
              <div className="sai-pricing-top">
                <div className="sai-pricing-tier">Starter</div>
                <div className="sai-pricing-price">
                  <sup>$</sup>100<sub>/mo</sub>
                </div>
                <p className="sai-pricing-desc">For individual developers and small teams getting started with AI-assisted editing.</p>
                <div className="sai-pricing-model sonnet">⚡ Claude Sonnet — Architect + Surgeon</div>
              </div>
              <div className="sai-pricing-divider"></div>
              <div className="sai-pricing-features">
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>Claude Sonnet</strong> for both Architect and Surgeon roles</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>GitHub integration</strong> — branch-aware edits, PR creation</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>Up to 5 files</strong> per session</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>SEARCH/REPLACE</strong> precision editing</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>QA auto-heal</strong> — 1 retry attempt on TS error</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>Real-time streaming</strong> — all pipeline stages</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>Standard support</strong></p></div>
                {/* Stripe: data-stripe-price-id="price_starter_monthly" */}
                <a href="/login" className="sai-pricing-cta starter-btn">Get Started</a>
              </div>
            </div>

            {/* PRO */}
            <div className="sai-pricing-card featured">
              <div className="sai-pricing-top">
                <div className="sai-pricing-popular">Most Powerful</div>
                <div className="sai-pricing-tier" style={{color:'var(--accent)'}}>Pro</div>
                <div className="sai-pricing-price">
                  <sup>$</sup>200<sub>/mo</sub>
                </div>
                <p className="sai-pricing-desc">For teams shipping fast. Maximum intelligence, unlimited scale, priority everything.</p>
                <div className="sai-pricing-model opus">🏛 Claude Opus — Architect + Surgeon</div>
              </div>
              <div className="sai-pricing-divider"></div>
              <div className="sai-pricing-features">
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>Claude Opus</strong> — Anthropic's most intelligent model for both roles</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>GitHub + Linear</strong> — full integration suite</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>Unlimited files</strong> per session</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>SEARCH/REPLACE + 32K direct rewrite</strong> for large files</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>QA auto-heal</strong> — 3 retry attempts, fresh error + live file each time</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>Priority queue</strong> at peak Anthropic traffic times</p></div>
                <div className="sai-pricing-feature"><span className="ck">✓</span><p><strong>Priority support</strong> — dedicated response SLA</p></div>
                {/* Stripe: data-stripe-price-id="price_pro_monthly" */}
                <a href="/login" className="sai-pricing-cta pro-btn">Get Pro</a>
              </div>
            </div>

          </div>
          <p className="sai-pricing-note">Prices shown exclude applicable tax. Both plans billed monthly. Cancel anytime.</p>
        </div>
      </section>

      {/* CTA */}
      <section className="sai-cta-section" id="contact">
        <div className="sai-cta-inner">
          {/* Left: copy */}
          <div className="sai-cta-copy">
            <h2>Ready to operate<br/>on your codebase?</h2>
            <p>SurgicalAI is in private access. Drop us a message and we'll get your team onboarded — usually within 24 hours.</p>
            <div className="sai-cta-detail">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"><polyline points="20 6 9 17 4 12"/></svg>
              Surgical precision — no full-file rewrites
            </div>
            <div className="sai-cta-detail">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"><polyline points="20 6 9 17 4 12"/></svg>
              GitHub + Linear integrations included
            </div>
            <div className="sai-cta-detail">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"><polyline points="20 6 9 17 4 12"/></svg>
              Claude Architect + Surgeon — zero GPT
            </div>
          </div>
          {/* Right: form card */}
          <div className="sai-form-card">
            {formSuccess ? (
              <div className="sai-form-success">
                <div className="sai-form-success-icon">
                  <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#0fa876" strokeWidth="2.5"><circle cx="12" cy="12" r="10"/><polyline points="20 6 9 17 4 12"/></svg>
                </div>
                <h3>Message sent!</h3>
                <p>We got it. Expect a reply within 24 hours.</p>
                <button className="sai-form-reset" onClick={() => setFormSuccess(false)}>Send another message</button>
              </div>
            ) : (
              <form onSubmit={handleContactSubmit} className="sai-contact-form">
                <div className="sai-form-row-2">
                  <div className="sai-field">
                    <label htmlFor="cf-name">Your name</label>
                    <input id="cf-name" type="text" name="name" placeholder="Alex Johnson" required/>
                  </div>
                  <div className="sai-field">
                    <label htmlFor="cf-email">Work email</label>
                    <input id="cf-email" type="email" name="email" placeholder="alex@company.com" required/>
                  </div>
                </div>
                <div className="sai-field">
                  <label htmlFor="cf-team">Team size</label>
                  <input id="cf-team" type="text" name="team_size" placeholder="e.g. 5 engineers, mono-repo"/>
                </div>
                <div className="sai-field">
                  <label htmlFor="cf-msg">Tell us about your codebase</label>
                  <textarea id="cf-msg" name="message" placeholder="What stack are you on? What's the biggest pain point today?" rows={4} required/>
                </div>
                {formError && <p className="sai-form-error">{formError}</p>}
                <button type="submit" className="sai-form-submit" disabled={formSubmitting}>
                  {formSubmitting ? (
                    <span style={{display:'flex',alignItems:'center',gap:'8px'}}>
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" style={{animation:'spin 1s linear infinite'}}><circle cx="12" cy="12" r="10" strokeOpacity="0.25"/><path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor"/></svg>
                      Sending…
                    </span>
                  ) : (
                    <>
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
                      Send Message
                    </>
                  )}
                </button>
              </form>
            )}
          </div>
        </div>
      </section>

      {/* FOOTER */}
      <footer className="sai-footer">
        <div style={{display:'flex',alignItems:'center',gap:'10px',fontSize:'15px',fontWeight:700,color:'var(--txt)'}}>
          <svg viewBox="0 0 28 28" width="22" height="22" fill="none">
            <rect width="28" height="28" rx="8" fill="url(#sai-g2)"/>
            <path d="M8 14 L13 9 L13 19" stroke="white" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/>
            <path d="M15 9 L20 14 L15 19" stroke="rgba(255,255,255,0.5)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/>
            <defs>
              <linearGradient id="sai-g2" x1="0" y1="0" x2="28" y2="28" gradientUnits="userSpaceOnUse">
                <stop stopColor="#6d5ce6"/><stop offset="1" stopColor="#0fa876"/>
              </linearGradient>
            </defs>
          </svg>
          SurgicalAI
        </div>
        <div className="sai-footer-links">
          <a href="#how-it-works">How it works</a>
          <a href="#features">Features</a>
          <a href="#integrations">Integrations</a>
          <a href="#pricing">Pricing</a>
          <a href="#compare">Compare</a>
          <a href="#contact">Contact</a>
        </div>
        <div>© 2026 SurgicalAI. All rights reserved.</div>
      </footer>
    </>
  );
}
