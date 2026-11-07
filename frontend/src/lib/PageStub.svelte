<script>
  export let label;
  export let hint;
</script>

<div class="stub">
  <h2 class="stub-title">{label}</h2>
  <p class="stub-hint">{hint}</p>
</div>

<style>
  .stub {
    padding: var(--synex-space-6);
    border: 1px dashed var(--synex-border-default);
    border-radius: var(--synex-radius-md);
    color: var(--synex-text-muted);
  }

  .stub-title {
    margin-top: 0;
    margin-right: 0;
    margin-bottom: var(--synex-space-2);
    margin-left: 0;
    font-family: var(--font-host);
    font-size: 18px;
    font-weight: 600;
    color: var(--synex-text-secondary);
  }

  .stub-hint {
    margin: 0;
  }
</style>