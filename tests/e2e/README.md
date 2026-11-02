# tests/e2e

End-to-end tests (W11): build a seed, boot a Ship on a test hypervisor pool, wait
for "needs sign-in", check the status document against the schema, reboot with an
auto and a bypass agent, then destroy the Ship. They need hypervisor access, so
`scripts/check` and CI do not run them. Both tracks contribute; nothing here yet.
