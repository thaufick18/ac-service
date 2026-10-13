document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.needs-validation').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!form.checkValidity()) {
        event.preventDefault();
        event.stopPropagation();
      }
      form.classList.add('was-validated');
    });
  });

  document.querySelectorAll('[data-password-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const input = document.getElementById(button.dataset.passwordToggle);
      if (!input) return;
      const showing = input.type === 'password';
      input.type = showing ? 'text' : 'password';
      button.setAttribute('aria-label', showing ? 'Hide password' : 'Show password');
      button.innerHTML = `<i class="bi ${showing ? 'bi-eye-slash' : 'bi-eye'}"></i>`;
    });
  });

  const serviceSelect = document.querySelector('[data-service-select]');
  const estimateOutput = document.querySelector('[data-estimate-output]');
  if (serviceSelect && estimateOutput) {
    const updateEstimate = () => {
      const price = serviceSelect.selectedOptions[0]?.dataset.price;
      estimateOutput.value = price ? Number(price).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : 'Select a service';
    };
    serviceSelect.addEventListener('change', updateEstimate);
    updateEstimate();
  }

  document.querySelectorAll('[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

});
