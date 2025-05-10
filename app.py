from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import os
import subprocess
import shutil
import zipfile
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = '/srv'
app.config['SERVICE_FILES_FOLDER'] = '/etc/systemd/system' # Bu dizin sudo yetkisi gerektirebilir
app.secret_key = 'super secret key' # Needed for flash messages

# Ana dizinler yoksa oluşturalım
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

def run_system_command(command_parts, successful_return_codes=None):
    """Helper function to run system commands and capture output."""
    if successful_return_codes is None:
        successful_return_codes = [0]

    try:
        # For commands that modify systemd, sudo is typically required.
        # Prepending 'sudo' to commands that need it.
        needs_sudo = False
        if command_parts[0] == 'systemctl' or \
           (command_parts[0] == 'rm' and app.config['SERVICE_FILES_FOLDER'] in command_parts[1]):
            needs_sudo = True

        cmd_to_run = list(command_parts) # Make a copy
        if needs_sudo and (len(cmd_to_run) == 0 or cmd_to_run[0] != 'sudo'):
            cmd_to_run.insert(0, 'sudo')

        result = subprocess.run(cmd_to_run, capture_output=True, text=True, check=False)
        
        is_ok = result.returncode in successful_return_codes

        if not is_ok:
            error_message = f"Error executing '{' '.join(cmd_to_run)}': RC={result.returncode}. Stderr: {result.stderr.strip()}"
            flash(error_message, 'error')
            print(error_message) # Also print to console for debugging
        
        return {
            "stdout": result.stdout.strip(), 
            "stderr": result.stderr.strip(), 
            "returncode": result.returncode, 
            "success": is_ok
        }

    except Exception as e:
        error_message = f"Exception executing '{' '.join(command_parts)}': {e}"
        flash(error_message, 'error')
        print(error_message)
        return {"stdout": "", "stderr": str(e), "returncode": -1, "success": False}

@app.route('/')
def index():
    services = get_systemd_services()
    return render_template('index.html', services=services)

def get_systemd_services():
    services = []
    if not os.path.exists(app.config['SERVICE_FILES_FOLDER']):
        flash(f"Service directory {app.config['SERVICE_FILES_FOLDER']} does not exist. Cannot list services.", 'error')
        return services

    try:
        # Ensure the user running this script has read access to SERVICE_FILES_FOLDER
        # For /etc/systemd/system, this might require sudo or specific group membership.
        # If using sudo for Flask app, this will work.
        # If not, this part may fail or return empty if permissions are insufficient.
        possible_service_files = os.listdir(app.config['SERVICE_FILES_FOLDER'])
    except PermissionError:
        flash(f"Permission denied when trying to read {app.config['SERVICE_FILES_FOLDER']}. Run with sudo or check permissions.", "error")
        return services
        
    for service_file in possible_service_files:
        if service_file.endswith(".service"):
            service_file_path = os.path.join(app.config['SERVICE_FILES_FOLDER'], service_file)
            
            status = "unknown"
            description = "N/A"
            working_dir = None
            service_user = "N/A"
            exec_start = "N/A" # Default ExecStart

            try:
                with open(service_file_path, 'r') as f:
                    for line in f:
                        line_strip = line.strip()
                        if line_strip.startswith("Description="):
                            description = line_strip.split("=", 1)[1]
                        elif line_strip.startswith("WorkingDirectory="):
                            working_dir = line_strip.split("=", 1)[1]
                        elif line_strip.startswith("User="):
                            service_user = line_strip.split("=", 1)[1]
                        elif line_strip.startswith("ExecStart="):
                            exec_start = line_strip.split("=", 1)[1]
                        
                        # Optimization: if all common fields found, no need to read further
                        if description != "N/A" and working_dir and service_user != "N/A" and exec_start != "N/A":
                            break
            except PermissionError:
                print(f"Permission denied reading {service_file_path}. Skipping for description/WD.")
                # flash(f"Error reading details for {service_file}: Permission denied.", "warning") # Too noisy
            except Exception as e:
                print(f"Could not read or parse {service_file_path} for details: {e}")
                # flash(f"Error reading details for {service_file}: {e}", "warning")

            # Only list services whose code appears to be managed by this tool
            # i.e., WorkingDirectory is set, exists, and is under our UPLOAD_FOLDER
            if working_dir and os.path.isdir(working_dir) and \
               os.path.abspath(working_dir).startswith(os.path.abspath(app.config['UPLOAD_FOLDER'])):

                status = "unknown" # Default status

                # Check active state
                active_cmd_res = run_system_command(['systemctl', 'is-active', service_file], successful_return_codes=[0, 3])

                if active_cmd_res["success"]:
                    service_stdout = active_cmd_res["stdout"]
                    service_rc = active_cmd_res["returncode"]
                    status = service_stdout # Base status from is-active output (e.g., "active", "inactive", "failed")

                    if service_rc == 3 and status != "failed": # if is-active reported not active and didn't already say "failed"
                        # Double-check with is-failed, as is-active might just say 'inactive' for a failed unit
                        failed_cmd_res = run_system_command(['systemctl', 'is-failed', service_file], successful_return_codes=[0, 1])
                        if failed_cmd_res["success"]:
                            if failed_cmd_res["returncode"] == 0: # Unit is genuinely failed
                                status = "failed"
                            # else: it's not failed, so status (likely "inactive") is correct.
                            if failed_cmd_res["stderr"]:
                                flash(f"Info from 'is-failed' for {service_file}: {failed_cmd_res['stderr']}", "warning")
                        else:
                            # Error running is-failed, this was already flashed by run_system_command
                            flash(f"Could not accurately determine 'failed' state for {service_file}. Status '{status}' may be incomplete.", "warning")
                    
                    if active_cmd_res["stderr"]:
                        # Flash warning if is-active produced stderr, even if RC was expected (e.g. unit not found if daemon not reloaded)
                        flash(f"Info from 'is-active' for {service_file}: {active_cmd_res['stderr']}", "warning")
                else:
                    # Error executing systemctl is-active (e.g. systemctl itself failed). Error was already flashed.
                    status = "error_checking_status"

                services.append({
                    "name": service_file,
                    "status": status,
                    "description": description,
                    "working_dir": working_dir,
                    "user": service_user,
                    "exec_start": exec_start # Add ExecStart to service details
                })
    return services

@app.route('/create_service', methods=['POST'])
def create_service():
    service_name_input = request.form.get('service_name')
    code_dir_input = request.form.get('code_dir_name') # New field
    description = request.form.get('description')
    exec_start = request.form.get('exec_start')
    service_user = request.form.get('service_user') # New field
    github_url = request.form.get('github_url')
    service_files_zip = request.files.get('service_files')

    if not all([service_name_input, description, exec_start]):
        flash("Servis adı, açıklama ve çalıştırılacak komut alanları zorunludur.", "error")
        return redirect(url_for('index'))

    service_name = secure_filename(service_name_input)
    if not service_name:
        flash("Geçersiz servis adı.", "error")
        return redirect(url_for('index'))

    # Determine the directory name for the service code
    if code_dir_input:
        code_folder_name = secure_filename(code_dir_input)
        if not code_folder_name: # If secure_filename made it empty (e.g., input was "../")
            code_folder_name = service_name # Fallback to service_name
            flash("Geçersiz kod dizini adı sağlandı, servis adı kullanılacak.", "warning")
    else:
        code_folder_name = service_name
    
    service_code_path = os.path.join(app.config['UPLOAD_FOLDER'], code_folder_name)
    service_unit_filepath = os.path.join(app.config['SERVICE_FILES_FOLDER'], f"{service_name}.service")

    # Check if service file or code directory already exists
    if os.path.exists(service_unit_filepath):
        flash(f"'{service_name}.service' adlı servis dosyası zaten mevcut. Lütfen farklı bir servis adı seçin.", "error")
        return redirect(url_for('index'))
    
    if os.path.exists(service_code_path):
        flash(f"'{service_code_path}' kod dizini zaten mevcut. Lütfen farklı bir kod dizini adı seçin veya mevcut olanı silin.", "error")
        return redirect(url_for('index'))

    try:
        os.makedirs(service_code_path, exist_ok=True)
    except OSError as e:
        flash(f"Kod dizini oluşturulamadı {service_code_path}: {e}", "error")
        return redirect(url_for('index'))

    # Source code handling
    if github_url:
        git_clone_result = run_system_command(['git', 'clone', github_url, service_code_path])
        if git_clone_result is None:
            shutil.rmtree(service_code_path, ignore_errors=True)
            return redirect(url_for('index'))
        flash(f"GitHub deposu {github_url} adresinden {service_code_path} dizinine klonlandı.", "success")
    elif service_files_zip and service_files_zip.filename != '':
        if not service_files_zip.filename.endswith('.zip'):
            flash("Yüklenen dosya ZIP arşivi olmalıdır.", "error")
            shutil.rmtree(service_code_path, ignore_errors=True)
            return redirect(url_for('index'))
        
        zip_filename = secure_filename(service_files_zip.filename)
        zip_filepath = os.path.join(service_code_path, zip_filename) # Save zip inside its code_path temporarily
        service_files_zip.save(zip_filepath)
        
        try:
            with zipfile.ZipFile(zip_filepath, 'r') as zip_ref:
                zip_ref.extractall(service_code_path)
            os.remove(zip_filepath)
            flash(f"{zip_filename} başarıyla yüklendi ve {service_code_path} dizinine çıkartıldı.", "success")
        except zipfile.BadZipFile:
            flash("Geçersiz veya bozuk ZIP dosyası.", "error")
            shutil.rmtree(service_code_path, ignore_errors=True)
            return redirect(url_for('index'))
        except Exception as e:
            flash(f"ZIP dosyası işlenirken hata oluştu: {e}", "error")
            shutil.rmtree(service_code_path, ignore_errors=True)
            return redirect(url_for('index'))
    else:
        flash("Servis kodu için bir GitHub Repo URL'si vermeli veya bir ZIP dosyası yüklemelisiniz.", "error")
        if os.path.exists(service_code_path): # Clean up the created directory if no code is provided
            shutil.rmtree(service_code_path, ignore_errors=True)
        return redirect(url_for('index'))

    # Change ownership of the service code directory if service_user is specified
    if service_user:
        chown_command = f"chown -R {service_user}:{service_user} '{service_code_path}'"
        chown_result = run_system_command(chown_command)
        if not chown_result['success']:
            flash(f"Uyarı: Servis kod dizininin ({service_code_path}) sahibi {service_user} olarak ayarlanamadı. Hata: {chown_result['stderr']}", 'warning')
        else:
            flash(f"Servis kod dizininin ({service_code_path}) sahibi başarıyla {service_user} olarak ayarlandı.", "info")

    # Create .service file
    # Ensure service_code_path is absolute for WorkingDirectory
    absolute_service_code_path = os.path.abspath(service_code_path)
    
    user_directive = f"User={service_user}" if service_user else "# User=your_user_name_here (not specified, runs as root by default)"

    service_file_content = f"""[Unit]
Description={description}
After=network.target

[Service]
ExecStart={exec_start}
WorkingDirectory={absolute_service_code_path}
{user_directive}
Restart=always
# Environment="PYTHONUNBUFFERED=1" # Example: if it's a Python app

[Install]
WantedBy=multi-user.target
"""
    # service_unit_filename = f"{service_name}.service" # Already defined
    # service_unit_filepath = os.path.join(app.config['SERVICE_FILES_FOLDER'], service_unit_filename) # Already defined

    try:
        # This write operation will require sudo if SERVICE_FILES_FOLDER is /etc/systemd/system
        # If the app isn't run with sudo, this will fail.
        # A more robust solution for non-sudo app involves a helper script with sudo rights
        # or configuring sudoers for passwordless execution of specific commands.
        with open(service_unit_filepath, 'w') as f:
            f.write(service_file_content)
        flash(f"Service file {service_unit_filepath} created at {service_unit_filepath}", "success")
    except PermissionError:
        flash(f"Permission denied: Could not write service file to {service_unit_filepath}. Run app with sudo or check permissions.", "error")
        shutil.rmtree(service_code_path, ignore_errors=True) # Clean up code if service file fails
        return redirect(url_for('index'))
    except Exception as e:
        flash(f"Error writing service file: {e}", "error")
        shutil.rmtree(service_code_path, ignore_errors=True) # Clean up
        return redirect(url_for('index'))

    # Reload systemd and enable the service
    daemon_reload_res = run_system_command(['systemctl', 'daemon-reload'])
    if daemon_reload_res["success"]:
        flash("Systemd daemon reloaded.", "info")
        
        # Optionally enable the service so it starts on boot
        # Ensure service_unit_filepath is used here if it's the full path
        enable_res = run_system_command(['systemctl', 'enable', service_unit_filepath]) # service_unit_filename is just 'name.service'
        if enable_res["success"]:
            flash(f"Service {service_unit_filepath} enabled.", "info")
        # else: error already flashed by run_system_command

    # else: error already flashed for daemon-reload

    return redirect(url_for('index'))

# Placeholder for other actions (start, stop, restart, delete)
@app.route('/start_service/<service_file_name>')
def start_service(service_file_name):
    safe_service_name = secure_filename(service_file_name)
    if not safe_service_name.endswith(".service"): # Basic check
        flash("Invalid service file name for start operation.", "error")
        return redirect(url_for('index'))
        
    start_res = run_system_command(['systemctl', 'start', safe_service_name])
    if start_res["success"]:
        flash(f"Service {safe_service_name} started.", "success")
    # else: error already flashed by run_system_command
    return redirect(url_for('index'))

@app.route('/stop_service/<service_file_name>')
def stop_service(service_file_name):
    safe_service_name = secure_filename(service_file_name)
    if not safe_service_name.endswith(".service"):
        flash("Invalid service file name for stop operation.", "error")
        return redirect(url_for('index'))

    stop_res = run_system_command(['systemctl', 'stop', safe_service_name])
    if stop_res["success"]:
        flash(f"Service {safe_service_name} stopped.", "success")
    # else: error already flashed
    return redirect(url_for('index'))

@app.route('/restart_service/<service_file_name>')
def restart_service(service_file_name):
    safe_service_name = secure_filename(service_file_name)
    if not safe_service_name.endswith(".service"):
        flash("Invalid service file name for restart operation.", "error")
        return redirect(url_for('index'))

    restart_res = run_system_command(['systemctl', 'restart', safe_service_name])
    if restart_res["success"]:
        flash(f"Service {safe_service_name} restarted.", "success")
    # else: error already flashed
    return redirect(url_for('index'))

@app.route('/delete_service/<service_file_name>')
def delete_service(service_file_name):
    safe_service_name = secure_filename(service_file_name)
    if not safe_service_name.endswith(".service"): 
        flash("Silme işlemi için geçersiz servis dosyası adı.", "error")
        return redirect(url_for('index'))
    
    service_unit_filepath = os.path.join(app.config['SERVICE_FILES_FOLDER'], safe_service_name)
    service_code_path = None

    if os.path.exists(service_unit_filepath):
        try:
            with open(service_unit_filepath, 'r') as f:
                for line in f:
                    if line.strip().startswith("WorkingDirectory="):
                        service_code_path = line.strip().split("=", 1)[1]
                        break
        except Exception as e:
            flash(f"{safe_service_name} servisinden Çalışma Dizini okunamadı: {e}. Kod dizini manuel olarak silinmelidir.", "warning")
    else:
        flash(f"Servis dosyası {service_unit_filepath} bulunamadı. Silme işlemine devam edilemiyor.", "error")
        return redirect(url_for('index'))

    # 1. Stop the service (best effort)
    run_system_command(['systemctl', 'stop', safe_service_name]) 

    # 2. Disable the service
    disable_res = run_system_command(['systemctl', 'disable', safe_service_name])
    # if disable_res["success"]: flash(f"Service {safe_service_name} disabled.", "info")

    # 3. Remove the service file
    if os.path.exists(service_unit_filepath):
        rm_res = run_system_command(['rm', service_unit_filepath]) 
        if rm_res["success"]:
            flash(f"Service file {safe_service_name} deleted.", "info")
        # else: error flashed by run_system_command
            
    # 4. Reload systemd daemon
    run_system_command(['systemctl', 'daemon-reload'])
    run_system_command(['systemctl', 'reset-failed']) 

    # 5. Remove the service code directory
    if os.path.isdir(service_code_path):
        try:
            shutil.rmtree(service_code_path)
            flash(f"Service code directory {service_code_path} deleted.", "info")
        except Exception as e:
            flash(f"Error deleting service code directory {service_code_path}: {e}", "error")
    
    flash(f"Service {safe_service_name} and its files have been processed for deletion.", "success")
    return redirect(url_for('index'))

@app.route('/update_service/<original_service_name>', methods=['POST'])
def update_service(original_service_name):
    if not original_service_name.endswith(".service"):
        flash("Güncellenecek servis adı geçersiz.", "error")
        return redirect(url_for('index'))

    safe_original_service_name = secure_filename(original_service_name)

    # Formdan verileri al
    new_description = request.form.get('description', '').strip()
    new_exec_start = request.form.get('exec_start', '').strip()
    new_service_user = request.form.get('service_user', '').strip()
    new_code_dir_name_form = request.form.get('code_dir_name', '').strip() # User's desired code_dir_name
    new_github_url = request.form.get('github_url', '').strip()
    new_service_files_zip = request.files.get('service_files')

    if not all([new_description, new_exec_start]):
        flash("Açıklama ve Çalıştırılacak Komut alanları zorunludur.", "error")
        # Redirect back to main page might lose context, ideally redirect to edit page or handle better
        return redirect(url_for('index')) 

    service_unit_filepath = os.path.join(app.config['SERVICE_FILES_FOLDER'], safe_original_service_name)

    if not os.path.exists(service_unit_filepath):
        flash(f"Servis dosyası {safe_original_service_name} bulunamadı. Güncelleme yapılamıyor.", "error")
        return redirect(url_for('index'))

    # Mevcut WorkingDirectory ve User bilgilerini oku (kodları/dizini yönetmek için)
    current_working_dir = None
    current_user = None # Belki ileride lazım olur
    try:
        with open(service_unit_filepath, 'r') as f:
            for line in f:
                if line.strip().startswith("WorkingDirectory="):
                    current_working_dir = line.strip().split("=", 1)[1]
                elif line.strip().startswith("User="):
                    current_user = line.strip().split("=", 1)[1]
                if current_working_dir and current_user:
                    break
    except Exception as e:
        flash(f"Mevcut servis dosyası ({safe_original_service_name}) okunurken hata: {e}", "error")
        return redirect(url_for('index'))

    if not current_working_dir:
        flash(f"Mevcut servis dosyasından ({safe_original_service_name}) WorkingDirectory okunamadı. Güncelleme güvenli değil.", "error")
        return redirect(url_for('index'))
    
    # Kod dizini adını ve yolunu belirle
    final_code_dir_name = secure_filename(new_code_dir_name_form) if new_code_dir_name_form else None
    
    # Eğer formda yeni bir kod dizini adı belirtilmemişse, mevcut WorkingDirectory'den dizin adını al
    if not final_code_dir_name and current_working_dir.startswith(os.path.abspath(app.config['UPLOAD_FOLDER'])):
        final_code_dir_name = os.path.basename(current_working_dir)
    elif not final_code_dir_name: # Mevcut WD de UPLOAD_FOLDER altında değilse veya bir şekilde belirlenemiyorsa
        flash("Yeni kod dizini adı belirtilmedi ve mevcut çalışma dizininden çıkarılamadı.", "error")
        return redirect(url_for('index'))
        
    new_service_code_path = os.path.join(app.config['UPLOAD_FOLDER'], final_code_dir_name)
    absolute_new_service_code_path = os.path.abspath(new_service_code_path)

    # Kaynak kodu güncelleme/değiştirme mantığı
    code_updated_or_moved = False
    if new_github_url or (new_service_files_zip and new_service_files_zip.filename != ''):
        if new_github_url:
            is_git_repo = os.path.isdir(os.path.join(absolute_new_service_code_path, '.git'))
            perform_clone = True # Varsayılan olarak klonla

            if is_git_repo:
                # Mevcut bir git deposu, remote URL'in eşleşip eşleşmediğini kontrol et
                get_remote_cmd = ['git', '-C', absolute_new_service_code_path, 'config', '--get', 'remote.origin.url']
                # Bu iç kontrol için run_system_command'ın hata flashlamasını istemiyoruz, sadece sonucu kontrol edeceğiz.
                # Ancak mevcut run_system_command yapısı her zaman flashlar. Şimdilik bu şekilde devam edelim.
                # Alternatif olarak, bu özel komut için subprocess.run doğrudan kullanılabilir.
                remote_res = run_system_command(get_remote_cmd) # successful_return_codes=[0] default

                if remote_res["success"] and remote_res["stdout"].strip() == new_github_url.strip():
                    # Remote URL eşleşiyor, git pull yapmayı dene
                    perform_clone = False
                    flash(f"Mevcut depo ({absolute_new_service_code_path}) için '{new_github_url}' adresinden güncellemeler çekiliyor (git pull)...", "info")
                    pull_cmd = ['git', '-C', absolute_new_service_code_path, 'pull']
                    pull_res = run_system_command(pull_cmd)
                    if pull_res["success"]:
                        flash(f"Depo '{absolute_new_service_code_path}' başarıyla güncellendi (git pull).", "success")
                        code_updated_or_moved = True
                    else:
                        # run_system_command zaten genel bir hata flashladı. Ekstra bağlam ekleyelim.
                        flash(f"'{absolute_new_service_code_path}' dizininde 'git pull' başarısız oldu. Lütfen çakışmaları manuel olarak çözün veya farklı bir kod dizini adı belirterek yeniden klonlamayı deneyin. Git çıktısı: {pull_res['stderr']}", "error")
                        return redirect(url_for('index')) # Güncelleme işlemini durdur
                else:
                    if not remote_res["success"]:
                        flash(f"Mevcut deponun ({absolute_new_service_code_path}) remote URL'i okunamadı. Depo yeniden klonlanacak. Git hatası: {remote_res['stderr']}", "warning")
                    else: # URL'ler eşleşmedi
                        flash(f"Yeni GitHub URL'i ({new_github_url}) mevcut depodaki remote ({remote_res['stdout'].strip()}) ile farklı. Depo yeniden klonlanacak.", "info")
                    # perform_clone zaten True
            
            if perform_clone:
                flash(f"'{new_github_url}' deposu '{absolute_new_service_code_path}' dizinine klonlanıyor...", "info")
                if os.path.exists(absolute_new_service_code_path):
                    try:
                        shutil.rmtree(absolute_new_service_code_path)
                    except Exception as e:
                        flash(f"Mevcut kod dizini ({absolute_new_service_code_path}) temizlenirken bir hata oluştu: {e}", "error")
                        return redirect(url_for('index'))
                
                try:
                    os.makedirs(absolute_new_service_code_path, exist_ok=True)
                except OSError as e:
                    flash(f"Yeni kod dizini ({absolute_new_service_code_path}) oluşturulurken bir hata oluştu: {e}", "error")
                    return redirect(url_for('index'))

                clone_cmd = ['git', 'clone', new_github_url, absolute_new_service_code_path]
                clone_res = run_system_command(clone_cmd)
                if clone_res["success"]:
                    flash(f"GitHub deposu '{new_github_url}' başarıyla '{absolute_new_service_code_path}' dizinine klonlandı.", "success")
                    code_updated_or_moved = True
                else:
                    # run_system_command zaten hatayı flashladı.
                    return redirect(url_for('index')) # Güncelleme işlemini durdur
        
        elif new_service_files_zip and new_service_files_zip.filename != '':
            flash(f"ZIP dosyası işleniyor, kodlar '{absolute_new_service_code_path}' dizininde güncellenecek...", "info")
            if os.path.exists(absolute_new_service_code_path):
                try:
                    shutil.rmtree(absolute_new_service_code_path)
                except Exception as e:
                    flash(f"Mevcut kod dizini ({absolute_new_service_code_path}) temizlenirken hata: {e}", "error")
                    return redirect(url_for('index'))
            
            try:
                os.makedirs(absolute_new_service_code_path, exist_ok=True)
            except OSError as e:
                flash(f"Yeni kod dizini ({absolute_new_service_code_path}) oluşturulamadı: {e}", "error")
                return redirect(url_for('index'))

            if not new_service_files_zip.filename.endswith('.zip'):
                flash("Yüklenen dosya ZIP arşivi olmalıdır.", "error")
                if os.path.exists(absolute_new_service_code_path): # Oluşturulmuş olabilir, temizle
                    shutil.rmtree(absolute_new_service_code_path, ignore_errors=True)
                return redirect(url_for('index'))
            
            zip_filename = secure_filename(new_service_files_zip.filename)
            # ZIP dosyasını geçici bir yerde saklamak yerine doğrudan UPLOAD_FOLDER ana dizinine kaydedip oradan işleyebiliriz.
            # Veya daha güvenli bir /tmp benzeri yer. Şimdilik UPLOAD_FOLDER ana dizini.
            temp_zip_filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"temp_{zip_filename}")
            new_service_files_zip.save(temp_zip_filepath)
            try:
                with zipfile.ZipFile(temp_zip_filepath, 'r') as zip_ref:
                    zip_ref.extractall(absolute_new_service_code_path) # Hedef dizine aç
                flash(f"{zip_filename} başarıyla {absolute_new_service_code_path} dizinine çıkartıldı.", "success")
                code_updated_or_moved = True
            except Exception as e:
                flash(f"ZIP dosyası işlenirken hata: {e}", "error")
                if os.path.exists(absolute_new_service_code_path): # Hedef dizini temizle
                     shutil.rmtree(absolute_new_service_code_path, ignore_errors=True)
                return redirect(url_for('index'))
            finally:
                if os.path.exists(temp_zip_filepath):
                    os.remove(temp_zip_filepath) # Geçici ZIP dosyasını sil
    elif absolute_new_service_code_path != os.path.abspath(current_working_dir):
        # Kod dizini adı değiştirildi ama yeni kaynak sağlanmadı.
        # Eski içeriği yeni yola taşımayı deneyebiliriz veya kullanıcıyı uyarabiliriz.
        # Şimdilik: Eğer yeni yol zaten yoksa ve eski yol varsa, taşı.
        if not os.path.exists(absolute_new_service_code_path) and os.path.exists(current_working_dir):
            try:
                shutil.move(current_working_dir, absolute_new_service_code_path)
                flash(f"Kod dizini {current_working_dir} adresinden {absolute_new_service_code_path} adresine taşındı.", "info")
                code_updated_or_moved = True
            except Exception as e:
                flash(f"Kod dizini taşınırken hata: {e}. Manuel taşıma gerekebilir.", "warning")
        elif os.path.exists(absolute_new_service_code_path):
             flash(f"Yeni kod dizini ({absolute_new_service_code_path}) zaten mevcut. Kod taşınmadı.", "warning")
        else: # current_working_dir yoksa (beklenmedik durum)
            flash(f"Eski kod dizini ({current_working_dir}) bulunamadı. Kod taşınmadı.", "warning")
            # Yeni kod dizini yine de oluşturulabilir (eğer yoksa) ve boş olabilir.
            if not os.path.exists(absolute_new_service_code_path):
                 os.makedirs(absolute_new_service_code_path, exist_ok=True)

    # Yeni .service dosya içeriğini oluştur
    user_directive_line = f"User={new_service_user}" if new_service_user else ""

    # .service dosyasından tüm satırları oku, sonra istediklerimizi değiştirerek yeni içeriği oluştur
    updated_lines = []
    service_file_found_directives = {"Description": False, "ExecStart": False, "WorkingDirectory": False, "User": False}

    try:
        with open(service_unit_filepath, 'r') as f:
            for line in f:
                stripped_line = line.strip()
                if stripped_line.startswith("Description="):
                    updated_lines.append(f"Description={new_description}\n")
                    service_file_found_directives["Description"] = True
                elif stripped_line.startswith("ExecStart="):
                    updated_lines.append(f"ExecStart={new_exec_start}\n")
                    service_file_found_directives["ExecStart"] = True
                elif stripped_line.startswith("WorkingDirectory="):
                    updated_lines.append(f"WorkingDirectory={absolute_new_service_code_path}\n")
                    service_file_found_directives["WorkingDirectory"] = True
                elif stripped_line.startswith("User="):
                    if new_service_user: # Sadece yeni kullanıcı belirtilmişse User satırını ekle/güncelle
                        updated_lines.append(f"User={new_service_user}\n")
                    service_file_found_directives["User"] = True # Orijinalde var olarak işaretle (kaldırmak için değil)
                else:
                    updated_lines.append(line) # Diğer satırları olduğu gibi koru
        
        # Eğer bazı direktifler dosyada yoksa (örn. User satırı hiç yoktu), [Service] altına ekle
        # Bu daha karmaşık bir ayrıştırma gerektirir. Şimdilik, var olanları güncellemeyle yetinelim.
        # Eğer User direktifi yoktu ve yenisi eklendiyse, doğru yere eklendiğinden emin olmalıyız.
        # Basitlik adına, eğer User satırı yoksa ve new_service_user varsa, bunu ExecStart'tan sonra ekleyelim.
        if not service_file_found_directives["User"] and new_service_user:
            # Find ExecStart and insert User after it
            temp_lines = []
            inserted_user = False
            for i, l in enumerate(updated_lines):
                temp_lines.append(l)
                if l.strip().startswith("ExecStart=") and not inserted_user:
                    temp_lines.append(f"User={new_service_user}\n")
                    inserted_user = True
            if inserted_user:
                updated_lines = temp_lines
            else: # ExecStart bulunamazsa (çok olası değil ama), [Service] altına en sona ekle (riskli)
                # Daha iyi bir yol, [Service] bloğunu bulup sonuna eklemek olurdu.
                # Şimdilik, eğer ExecStart yoksa User ekleme işlemini atla ve uyarı ver
                flash("User direktifi eklenecek uygun yer bulunamadı (.service dosyasında ExecStart yok).", "warning")

        updated_service_file_content = "".join(updated_lines)

    except Exception as e:
        flash(f"Servis dosyası ({safe_original_service_name}) güncellenmek üzere okunurken hata: {e}", "error")
        return redirect(url_for('index'))

    # .service dosyasını yaz
    try:
        with open(service_unit_filepath, 'w') as f:
            f.write(updated_service_file_content)
        flash(f"Servis dosyası {safe_original_service_name} güncellendi.", "success")
    except PermissionError:
        flash(f"İzin hatası: {service_unit_filepath} yazılamadı. sudo ile çalıştırın veya izinleri kontrol edin.", "error")
        return redirect(url_for('index'))
    except Exception as e:
        flash(f"Servis dosyası yazılırken hata: {e}", "error")
        return redirect(url_for('index'))

    # Systemd'yi yeniden yükle
    daemon_reload_res = run_system_command(['systemctl', 'daemon-reload'])
    if daemon_reload_res["success"]:
        flash("Systemd daemon yeniden yüklendi. Değişikliklerin etkili olması için servisi yeniden başlatmanız gerekebilir.", "info")
    else:
        flash("Systemd daemon yeniden yüklenemedi. Değişiklikler etkili olmayabilir.", "warning")

    return redirect(url_for('index'))

@app.route('/get_service_logs/<service_file_name>')
def get_service_logs(service_file_name):
    safe_service_name = secure_filename(service_file_name)
    if not safe_service_name.endswith(".service"):
        return jsonify({"error": "Geçersiz servis adı"}), 400

    # Fetch last 100 lines, newest first. --no-hostname omits hostname from each line.
    # Consider adding --output cat for very plain output if needed, but default should be fine.
    log_command_res = run_system_command(
        ['journalctl', '-u', safe_service_name, '-n', '300', '--no-pager', '--reverse'],
        successful_return_codes=[0] # journalctl should return 0 if logs are found or not (empty set is not an error for it)
    )

    if log_command_res["success"]:
        # If stdout is empty but stderr has "No entries", it means no logs, which is fine.
        if not log_command_res["stdout"] and "No entries" in log_command_res["stderr"]:
             return jsonify({"logs": "-- Bu servis için henüz log kaydı bulunmuyor. --"})
        elif log_command_res["stdout"]:
            return jsonify({"logs": log_command_res["stdout"]})
        else:
            # If stdout is empty and stderr has something else, or is empty, it might be an issue.
            error_output = log_command_res["stderr"] if log_command_res["stderr"] else "Bilinmeyen bir hata oluştu veya log yok."
            # In this case, we might still want to return 200 OK with the error message in logs, 
            # as the command itself didn't fail based on successful_return_codes.
            # However, for journalctl, an empty stdout usually means no logs or an issue described in stderr.
            # For more clarity, we could treat unexpected stderr as an error for the client.
            if log_command_res["stderr"] and "No entries" not in log_command_res["stderr"]:
                 return jsonify({"error": f"Loglar alınırken bir sorun oluştu: {log_command_res['stderr']}"}), 500
            return jsonify({"logs": "-- Log bulunamadı veya boş. --"})

    else:
        # run_system_command already flashed an error for the main page.
        # For an AJAX request, we should return a JSON error.
        error_detail = log_command_res['stderr'] or "journalctl komutu çalıştırılamadı."
        return jsonify({"error": f"Loglar alınamadı: {error_detail}"}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5554) 