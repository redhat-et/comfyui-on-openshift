# The parts that cannot be scripted

Everything in this file is browser-only. It takes about 20 minutes. Everything
after it is scripted.

## 1. Create the AWS account (~10 min)

<https://portal.aws.amazon.com/billing/signup>

- Root email must be one you control and that is not already an AWS root
  account. Use a plus-address (`you+rosa@domain`) so you can tell it apart later.
- Requires: credit card, phone number (SMS or voice code), email code.
- Pick the **Basic** support plan at signup. You will upgrade in step 3 —
  do not pay for Business before the account is even usable.
- Account creation takes a few minutes to finish activating. GPU quota
  requests filed before activation completes get auto-rejected, so wait for
  the "your account is ready" email.

## 2. Harden root, then stop using it (~5 min)

In the console as root:

- IAM → Security credentials → **enable MFA on the root user**.
- Do **not** create root access keys. If AWS offers, decline.
- Create one IAM admin user for yourself with console + programmatic access:
  that is what `01-bootstrap-account.sh` expects to run as.

Fast path: root console → IAM → Users → Create user → attach
`AdministratorAccess` → create access key of type "Command Line Interface".
Then `aws configure --profile rosa-admin` locally and never log in as root again.

That profile name is what `.env` expects as `AWS_PROFILE`.

## 3. Decide about the support plan

The two vendors word this differently, and it matters:

- **AWS's ROSA setup page** lists a **Business**, **Enterprise On-Ramp**, or
  **Enterprise** support plan under prerequisites.
- **Red Hat's ROSA prerequisites** say Red Hat *recommends* at least Business
  support.

In practice clusters do build on Basic. What you lose is the escalation path:
when Red Hat SRE needs to open an AWS case about your cluster, there is no case
to open. For a throwaway test cluster that may be an acceptable trade; for
anything you depend on it is not.

Business is the greater of $100/month or 10% of the first $10K of monthly usage
— about $150/month at this cluster's burn rate, and it bills whether or not a
cluster exists.

Console → Support → Support plans → Business.

**This is the strongest argument for a Red Hat-provided AWS account**, where the
org's plan already covers you. See the top of `README.md` before paying for it
yourself.

Going the self-managed Single Node OpenShift route instead? Skip this entirely —
the question is ROSA's, not OpenShift's.

## 4. Enable ROSA in the console (~2 min)

<https://console.aws.amazon.com/rosa>

- **Get started**
- Check *I agree to share my contact information with Red Hat*
- **Enable ROSA**

This one click does three things you cannot do from the CLI: accepts the AWS
Marketplace terms for the ROSA product, creates the
`AWSServiceRoleForElasticLoadBalancing` service-linked role, and runs the
prerequisite verifier.

## 5. Link your Red Hat account and grab an offline token

<https://console.redhat.com/openshift/token/rosa>

Log in with your Red Hat SSO (your work account). Copy the offline token into
`.env` as `ROSA_TOKEN=...`.

---

## Then

```bash
cp .env.example .env && $EDITOR .env
make tools
make preflight     # tells you which of steps 1-5 are actually done
make account       # files the quota requests — do this immediately
```

`make preflight` checks every one of the above from the outside, including the
support plan, so you do not have to trust your own memory of what you clicked.
