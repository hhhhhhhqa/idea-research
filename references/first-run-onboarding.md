# First Run — Onboarding

If the user has no existing profile preference, ask the following questions in order. Keep each question short. Defaults are listed so the Agent can proceed when the user says “默认”.

1. **Introduction.** Explain that Idea Research reads newsletters, buy-side and company-analysis voices on X, WSB discussion and U.S. close data. It shows who said what; it does not issue trade recommendations.
2. **Frequency and timezone.** Ask daily or weekly, then desired delivery time and IANA timezone. Default: daily at `07:30`, `Asia/Hong_Kong`. For weekly, also ask the day.
3. **Language.** Ask Chinese, English or bilingual. Default: Chinese.
4. **Detail.** Ask highlights, standard or full. Default: standard.
5. **Research scope.** Ask which of AI infrastructure, Internet platforms, SaaS, cybersecurity, advertising, or named tickers deserve extra attention. Keep the default broad AI / Internet / software scope if the user has no preference.
6. **Delivery.** If the current Agent is persistent and the user asks for delivery, configure that platform's own scheduler after confirming it can send messages. Otherwise state clearly that this session can generate an on-demand digest but cannot promise unattended delivery.

Store preferences in a user-specific YAML file under `config/profiles/` and pass it with `--profile`; do not overwrite `default.yaml` when multiple users share a checkout. Never write delivery secrets into Git.

After saving the profile, do not wait for the requested schedule. Immediately run the digest workflow and ask the user whether the first report is too long, too short, or should cover different companies.
