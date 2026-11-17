package ru.lenainbox.alerts;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;

/** Independent recovery check for queued events when immediate scheduling is lost. */
public final class WatchdogJob extends JobService {
    private static final int WATCHDOG_ID = 31073;
    private static final long CHECK_INTERVAL_MILLIS = 15 * 60 * 1000L;

    static synchronized void ensure(Context context) {
        JobScheduler scheduler = (JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        for (JobInfo pending : scheduler.getAllPendingJobs()) {
            if (pending.getId() == WATCHDOG_ID) return;
        }
        JobInfo job = new JobInfo.Builder(WATCHDOG_ID, new ComponentName(context, WatchdogJob.class))
                .setPeriodic(CHECK_INTERVAL_MILLIS)
                .setPersisted(true)
                .build();
        if (scheduler.schedule(job) != JobScheduler.RESULT_SUCCESS) {
            context.getSharedPreferences("diagnostics", MODE_PRIVATE).edit()
                    .putString("delivery", "Android не запланировал проверку очереди").apply();
        }
    }

    @Override public boolean onStartJob(JobParameters params) {
        new Thread(() -> {
            try (EventStore store = new EventStore(this)) {
                if (store.count() > 0) SendJob.schedule(this);
            } catch (Exception failure) {
                getSharedPreferences("diagnostics", MODE_PRIVATE).edit()
                        .putString("delivery", "Не удалось проверить очередь").apply();
            } finally {
                jobFinished(params, false);
            }
        }, "alert-watchdog").start();
        return true;
    }

    @Override public boolean onStopJob(JobParameters params) { return true; }
}
