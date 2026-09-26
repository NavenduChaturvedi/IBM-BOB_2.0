**Roll back if:**

- Error rate on the checkout service exceeds 1% for 5 minutes.

1. **Roll back the checkout service.**

   ```bash
   kubectl rollout undo deployment/checkout -n prod
   ```

2. **Roll back the Helm release** to the previous revision.

   ```bash
   helm rollback payments-api 1
   ```

3. **Revert the merge commit.**

   ```bash
   git revert -m 1 <merge-sha>
   git push
   ```

4. **Notify the status page.**

   ```bash
   curl -X POST https://status.example.com/api/incidents -d '{"status": "resolved"}'
   ```
