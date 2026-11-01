# Security reporting

Do not put credentials, account identifiers, native configuration, raw logs or process tokens in a public issue. Use a sanitized diagnostic preview for ordinary compatibility/cleanup bugs.

A dedicated private security intake address or enabled advisory channel has not yet been verified. Before disclosing a sensitive report, ask the maintainer through an existing trusted private contact for an appropriate channel; do not infer an email address or assume GitHub private advisories are enabled. This release prerequisite remains documented rather than inventing a reporting destination.

Include affected commit/version, environment, relevant trust boundary and a synthetic reproduction that does not control a real account. Source/history sanitization, helper authentication, process ownership and execution-isolation problems should be treated as sensitive. The application does not upload diagnostic bundles automatically.
