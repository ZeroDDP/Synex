<!-- Единственный источнеик правды -->
<script>
  // Пропсы из App  
  export let items;
  export let active;
  export let onSelect;
</script>

<aside class="sidebar">
  <header class="logo">
    <span class="logo-mark" aria-hidden="true"></span>
    <span class="logo-text">SYNEX</span>
  </header>

  <nav class="nav" aria-label="Основная навигация">
    {#each items as item (item.id)}
      <button
        type="button"
        class="nav-btn"
        class:nav-btn--active={item.id === active}
        on:click={() => onSelect(item.id)}
      >
        {item.label}
      </button>
    {/each}
  </nav>

  <footer class="user">
    <span class="avatar" aria-hidden="true"></span>
    <span class="username">Username</span>
    <span class="dot" aria-hidden="true"></span>
  </footer>
</aside>
<style>
  .sidebar {
    display: flex;
    flex-direction: column;
    flex: 0 0 var(--synex-sidebar-width);
    background: var(--synex-bg-surface);
    border-right: 1px solid var(--synex-border-subtle);
  }

  .logo {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: var(--synex-space-3);
    padding: var(--synex-space-4);
    border-bottom: 1px solid var(--synex-border-subtle);
    height: 60px;
  }

  /* Заглушки для лого  */
  .logo-mark {
    width: 28px;
    height: 28px;
    border-radius: var(--synex-radius-sm);
    background: var(--synex-accent);
    
  }

  .logo-text {
    font-family: var(--font-inter);
    font-size: 15px;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--synex-text-primary);
  }


  .nav {
    flex: 1 1 auto; /* Сайдбар растягивается по высоте */
    overflow-y: auto; /* Прокрутка сайдбара если не поместился текст */
    display: flex;
    flex-direction: column;
    gap: var(--synex-space-1);
    padding: var(--synex-space-3) var(--synex-space-2);
  }

  .nav-btn {
    display: flex;
    align-items: center;
    gap: var(--synex-space-2);
    padding: var(--synex-space-2) var(--synex-space-3);
    border: none;
    border-radius: var(--synex-radius-sm);
    background: transparent;
    color: var(--synex-accent);
    font-family: var(--font-inter);
    font-size: 14px;
    text-align: left;
    cursor: pointer;
    font-weight: bold;  
    height: 55px;

  }
  

  .nav-btn:hover {
    background: var(--synex-bg-hover);
  }

  .nav-btn--active {
    font-family: var(--font-inter);
    background: var(--synex-bg-active);
  }

  .user {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: var(--synex-space-2);
    padding: var(--synex-space-3) var(--synex-space-4);
    border-top: 1px solid var(--synex-border-subtle);
  }

  .avatar {
    flex: 0 0 auto;
    width: 32px;
    height: 32px;
    border-radius: 50%;
    background: var(--synex-border-strong);
  }

  .username {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 13px;
    color: var(--synex-text-primary);
    font-family: var(--font-host);
  }

  .dot {
    flex: 0 0 auto;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--synex-accent);
  }
</style>