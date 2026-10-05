// 密码确认验证
const passwordForm = document.querySelector('form[action="/settings/password"]');
if (passwordForm) {
    passwordForm.addEventListener('submit', function(e) {
        const newPassword = document.getElementById('new_password').value;
        const confirmPassword = document.getElementById('confirm_password').value;
        
        if (newPassword !== confirmPassword) {
            e.preventDefault();
            alert('两次输入的新密码不一致');
            return false;
        }
        
        if (newPassword && newPassword.length < 6) {
            e.preventDefault();
            alert('新密码长度不能少于6位');
            return false;
        }
    });
}
