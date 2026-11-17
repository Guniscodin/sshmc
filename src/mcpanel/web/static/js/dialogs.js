let confirmDialogSequence = 0;

function sshmcConfirm({ title, message, details = [], confirmLabel = 'Continue', danger = false }) {
    return new Promise(resolve => {
        const dialog = document.createElement('dialog');
        dialog.className = 'sshmc-confirm-dialog';
        const dialogId = `sshmc-confirm-${++confirmDialogSequence}`;
        const heading = document.createElement('h2');
        heading.id = `${dialogId}-title`;
        heading.textContent = title;
        dialog.setAttribute('aria-labelledby', heading.id);
        const body = document.createElement('p');
        body.id = `${dialogId}-description`;
        body.textContent = message;
        dialog.setAttribute('aria-describedby', body.id);
        const list = document.createElement('ul');
        for (const detail of details) {
            const item = document.createElement('li');
            item.textContent = detail;
            list.append(item);
        }
        const actions = document.createElement('div');
        actions.className = 'dialog-actions';
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'btn btn-ghost';
        cancel.textContent = 'Cancel';
        const confirm = document.createElement('button');
        confirm.type = 'button';
        confirm.className = danger ? 'btn btn-danger' : 'btn btn-primary';
        confirm.textContent = confirmLabel;
        actions.append(cancel, confirm);
        dialog.append(heading, body);
        if (details.length) dialog.append(list);
        dialog.append(actions);
        document.body.append(dialog);
        let settled = false;
        const finish = accepted => {
            if (settled) return;
            settled = true;
            if (dialog.open) dialog.close();
            dialog.remove();
            resolve(accepted);
        };
        cancel.addEventListener('click', () => finish(false));
        confirm.addEventListener('click', () => finish(true));
        dialog.addEventListener('cancel', event => { event.preventDefault(); finish(false); });
        dialog.addEventListener('click', event => { if (event.target === dialog) finish(false); });
        try {
            dialog.showModal();
            cancel.focus();
        } catch (error) {
            dialog.remove();
            resolve(false);
        }
    });
}

window.sshmcConfirm = sshmcConfirm;
if (window.sshmc) {
    window.sshmc.confirm = sshmcConfirm;
}
