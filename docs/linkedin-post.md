# LinkedIn Post: Decision Models in the Dev Workflow

> Hero image: `infographic/ai-decision-models/hero-image.png` (1200x627, sized for a LinkedIn post)
> Repo: https://github.com/adelvillar1/dev-decisions (the CLI + hooks)
> Library underneath: https://github.com/adelvillar1/sys1 (provider registry + decision engine)

---

## Final post

You commit at 11pm and the only reviewer is you. Push, ask for a review, wonder if the change is risky, hope you did not just leak a secret. Most of us handle that moment with a vibe and a shrug.

I got tired of the shrug, so I built a thing that puts a few models in front of the question. It is called dev-decisions. You commit, a git hook runs, and a set of decision models classify the diff and tell you what they think. Then you decide whether to listen.

That last part matters. This is not an agent that merges on your behalf. It is a second opinion that is hard to fool and never gets tired at 2am.

How it works, roughly: a provider takes your diff and answers a few questions. What kind of change is this (feat, fix, refactor)? How risky is it (low, medium, high)? Is it a breaking change? Each answer comes back with a confidence number. Under the floor, it escalates and says "you should look at this." Over the floor, it lets you through. The whole thing logs to JSONL, so over weeks you can go back and see where you were wrong.

The models themselves are the interesting part to me. I use four:

Jev is the careful one. It does not just answer, it gives you a probability distribution. A 0.7 confidence on a binary is not the same thing as a 0.7 on a five-way choice, and Jev knows that. It is also the only one that will tell you "I don't know" without pretending otherwise.

Decide (Fastino's GLiNER-2.5-Decide over their API) is what I reach for when I want a fast read. It declines on ambiguous diffs, which sounds like a weakness but is mostly a feature, because it is refusing to guess instead of shipping you a confident wrong answer.

Local is the same model in a uv venv on my machine. No API call, no cost, safe for repos I do not want sending anywhere. The tradeoff is setup: it wants Python 3.12 and a couple hundred megabytes of torch, so it lives on a Mac, not in a CI runner.

ModernBERT does not really decide anything. It is a sentence encoder that logs raw predictions so I can calibrate the rest. It turns my gut feel into a chart.

The places I have actually used it:

A git pre-commit hook is the obvious one. The hook scans your staged diff before it lands. You get the "are you sure?" nudge before you push, not after a teammate finds a stray API key in code review. I have shipped enough secrets in my career that I will take a 200ms pause for that.

Once a PR is open, it becomes a first-pass reviewer that never gets grumpy on a Monday. It classifies the change, flags risk, suggests labels. A human still merges, but the human starts from a better place.

The 1am solo case is what surprised me. You are the reviewer and the reviewed. A model with no stake in shipping the thing is a useful counterweight to your own "it's fine, it works."

And then over time, the logs. Every classification, every decision you ignored, every wrong prediction. That pile becomes a calibration set, and you can see whether a model is getting more accurate or just more confident. I did not expect to care about this. I now care about it a lot.

The honest downsides. Confidence is not certainty, and a model can be confidently wrong. A too-low floor drowns you in escalations, a too-high one lets the dangerous stuff through. None of this removes the need for tests. If you have no tests, a model cannot tell you whether your change is right, only what it smells like.

AI in developer tools gets mostly discussed as a generator. Make the code, finish the function, write the test. The support role is more interesting to me. These models do not write the diff. They look at the diff and ask a better question than "lgtm?"

The code is open source at https://github.com/adelvillar1/dev-decisions. The library underneath, sys1, is at https://github.com/adelvillar1/sys1. Both are Apache 2.0, both are documented, but the call to ship is still yours. That part matters more than the rest.

Curious how many of you let a model gate your commits, and what made you turn it off.

---

## Hashtags

Pick 3-5, not all of them. Mix reach and niche.

```
#DevOps #SoftwareEngineering #Git #MachineLearning #DeveloperExperience #CodeQuality #AICodeReview #DeveloperProductivity #MLOps #OpenSource #SecureByDefault #TypeScript #Python
```

By audience:

- General dev reach: `#DevOps #SoftwareEngineering #Git #DeveloperProductivity #OpenSource`
- Targeted / dev-tooling crowd: `#DeveloperExperience #CodeQuality #DeveloperProductivity #MLOps #Git`
- AI/ML angle: `#AICodeReview #MachineLearning #MLOps #DecisionSupport #ModelCalibration`
- Security angle: `#DevSecOps #SecureByDefault #AppSec #SecretsDetection`

## Image notes

`hero-image.png` is 1200x627, the LinkedIn recommended link-preview size. The SVG source is next to it if you want to tweak colors or text.

## Formatting notes

- First 1-2 lines are the hook, so they render in the feed preview before "see more."
- Short paragraphs, no subheads. LinkedIn collapses long blocks.
- One image only. The infographic does the visual work.
- No emoji in the body. They read as AI to most people now.
- LinkedIn's algorithm penalizes outbound links in the body. Consider posting with the URLs removed, then adding them in the first comment. The body already has the links so you can copy them out.
