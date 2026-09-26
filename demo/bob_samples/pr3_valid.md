**Roll back if:**

- Logs show `TypeError: calculate_discount() missing 1 required positional argument: 'user_tier'`. Both app callers were updated, so this means a caller the static search missed.
- 5xx rises on checkout or cart preview (`post_checkout`, `get_cart_preview` in app/api/routes.py): every checkout goes through `calculate_discount`.

1. **Confirm the failure is this PR.** Look for the signature error in the last 10 minutes of logs.

   ```bash
   kubectl logs deployment/api -n payments --since=10m | grep "calculate_discount"
   ```

   Check: if nothing matches and errors are elsewhere, this PR is probably not the cause. Stop and escalate.

2. **Roll back `deployment/api`.** No migration or flag is involved, so a workload rollback is the complete mitigation.

   ```bash
   kubectl rollout undo deployment/api -n payments
   kubectl rollout status deployment/api -n payments
   ```

   Check: rollout reports success and checkout 5xx returns to baseline before continuing.

3. **Revert both commits** so the next deploy from main doesn't bring the change back.

   ```bash
   git revert --no-edit 772899608e20 57a607da8b6c
   git push
   ```

   Check: the revert touches app/payments/pricing.py, app/api/checkout.py, and app/api/cart.py together. Reverting only pricing.py would leave the callers passing `user_tier` to a 2-argument function.

4. **Verify** with the tests that cover `calculate_discount`:

   ```bash
   pytest tests/test_pricing.py
   ```

   Check: both tests pass again (they were written against the 2-argument signature).
