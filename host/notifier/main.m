#import <Cocoa/Cocoa.h>
#import <UserNotifications/UserNotifications.h>

static NSString *const VicegerentNotifierVersion = @"1.0.0";

@interface VicegerentNotificationDelegate : NSObject <UNUserNotificationCenterDelegate>
@end

@implementation VicegerentNotificationDelegate

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
       willPresentNotification:(UNNotification *)notification
         withCompletionHandler:(void (^)(UNNotificationPresentationOptions options))completionHandler
{
    completionHandler(
        UNNotificationPresentationOptionBanner |
        UNNotificationPresentationOptionList |
        UNNotificationPresentationOptionSound
    );
}

@end

static void finish(int status, NSString *message)
{
    if (message != nil) {
        FILE *stream = status == 0 ? stdout : stderr;
        fprintf(stream, "%s\n", message.UTF8String);
        fflush(stream);
    }
    exit(status);
}

static NSString *authorizationName(UNAuthorizationStatus status)
{
    switch (status) {
        case UNAuthorizationStatusNotDetermined:
            return @"not-determined";
        case UNAuthorizationStatusDenied:
            return @"denied";
        case UNAuthorizationStatusAuthorized:
            return @"authorized";
        case UNAuthorizationStatusProvisional:
            return @"provisional";
    }
    return @"unknown";
}

static void requireAuthorization(
    UNUserNotificationCenter *center,
    void (^authorizedHandler)(void)
)
{
    [center requestAuthorizationWithOptions:(UNAuthorizationOptionAlert | UNAuthorizationOptionSound)
                          completionHandler:^(BOOL granted, NSError *error) {
        if (error != nil) {
            if (
                [error.domain isEqualToString:UNErrorDomain] &&
                error.code == UNErrorCodeNotificationsNotAllowed
            ) {
                finish(4, @"notification authorization denied; enable Vicegerent in System Settings > Notifications");
            }
            finish(3, [NSString stringWithFormat:@"notification authorization failed: %@", error]);
        }
        if (!granted) {
            finish(4, @"notification authorization denied; enable Vicegerent in System Settings > Notifications");
        }
        [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
            if (settings.authorizationStatus != UNAuthorizationStatusAuthorized) {
                finish(
                    4,
                    [NSString stringWithFormat:
                        @"notification authorization is %@; enable Vicegerent in System Settings > Notifications",
                        authorizationName(settings.authorizationStatus)
                    ]
                );
            }
            if (settings.alertSetting != UNNotificationSettingEnabled) {
                finish(4, @"notification alerts are disabled; enable alerts for Vicegerent in System Settings > Notifications");
            }
            if (settings.alertStyle != UNAlertStyleAlert) {
                finish(4, @"notification style is not persistent; select Persistent under System Settings > Notifications > Vicegerent > Alert Style");
            }
            authorizedHandler();
        }];
    }];
}

static void verifyDelivered(
    UNUserNotificationCenter *center,
    NSString *identifier,
    NSUInteger attemptsRemaining
)
{
    [center getDeliveredNotificationsWithCompletionHandler:^(NSArray<UNNotification *> *notifications) {
        for (UNNotification *notification in notifications) {
            if ([notification.request.identifier isEqualToString:identifier]) {
                finish(0, nil);
            }
        }
        if (attemptsRemaining == 0) {
            finish(7, [NSString stringWithFormat:@"notification %@ was accepted but did not appear in Notification Center", identifier]);
        }
        dispatch_after(
            dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.1 * NSEC_PER_SEC)),
            dispatch_get_main_queue(),
            ^{ verifyDelivered(center, identifier, attemptsRemaining - 1); }
        );
    }];
}

static void postNotification(
    UNUserNotificationCenter *center,
    NSString *identifier,
    NSString *title,
    NSString *message
)
{
    [center getDeliveredNotificationsWithCompletionHandler:^(NSArray<UNNotification *> *notifications) {
        for (UNNotification *notification in notifications) {
            if ([notification.request.identifier isEqualToString:identifier]) {
                finish(0, nil);
            }
        }

        requireAuthorization(center, ^{
            UNMutableNotificationContent *content = [UNMutableNotificationContent new];
            content.title = title;
            content.body = message;

            UNNotificationRequest *request = [UNNotificationRequest
                requestWithIdentifier:identifier
                content:content
                trigger:nil
            ];
            __weak UNUserNotificationCenter *weakCenter = center;
            [center addNotificationRequest:request withCompletionHandler:^(NSError *deliveryError) {
                if (deliveryError != nil) {
                    finish(6, [NSString stringWithFormat:@"notification delivery failed: %@", deliveryError]);
                }
                dispatch_after(
                    dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.1 * NSEC_PER_SEC)),
                    dispatch_get_main_queue(),
                    ^{ verifyDelivered(weakCenter, identifier, 30); }
                );
            }];
        });
    }];
}

static void verifyRemoved(
    UNUserNotificationCenter *center,
    NSString *identifier,
    NSUInteger attemptsRemaining
)
{
    [center getDeliveredNotificationsWithCompletionHandler:^(NSArray<UNNotification *> *notifications) {
        BOOL stillDelivered = NO;
        for (UNNotification *notification in notifications) {
            if ([notification.request.identifier isEqualToString:identifier]) {
                stillDelivered = YES;
                break;
            }
        }
        if (!stillDelivered) {
            finish(0, nil);
        }
        if (attemptsRemaining == 0) {
            finish(7, [NSString stringWithFormat:@"notification %@ is still delivered", identifier]);
        }
        dispatch_after(
            dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.1 * NSEC_PER_SEC)),
            dispatch_get_main_queue(),
            ^{ verifyRemoved(center, identifier, attemptsRemaining - 1); }
        );
    }];
}

static void removeNotification(UNUserNotificationCenter *center, NSString *identifier)
{
    [center removePendingNotificationRequestsWithIdentifiers:@[identifier]];
    [center removeDeliveredNotificationsWithIdentifiers:@[identifier]];
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.1 * NSEC_PER_SEC)),
        dispatch_get_main_queue(),
        ^{ verifyRemoved(center, identifier, 20); }
    );
}

static void verifyAllRemoved(
    UNUserNotificationCenter *center,
    NSUInteger attemptsRemaining
)
{
    [center getDeliveredNotificationsWithCompletionHandler:^(NSArray<UNNotification *> *notifications) {
        if (notifications.count == 0) {
            finish(0, nil);
        }
        if (attemptsRemaining == 0) {
            finish(7, @"notifications are still delivered");
        }
        dispatch_after(
            dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.1 * NSEC_PER_SEC)),
            dispatch_get_main_queue(),
            ^{ verifyAllRemoved(center, attemptsRemaining - 1); }
        );
    }];
}

static void removeAllNotifications(UNUserNotificationCenter *center)
{
    [center removeAllPendingNotificationRequests];
    [center removeAllDeliveredNotifications];
    dispatch_after(
        dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.1 * NSEC_PER_SEC)),
        dispatch_get_main_queue(),
        ^{ verifyAllRemoved(center, 20); }
    );
}

static void listNotifications(UNUserNotificationCenter *center)
{
    [center getDeliveredNotificationsWithCompletionHandler:^(NSArray<UNNotification *> *notifications) {
        for (UNNotification *notification in notifications) {
            printf(
                "%s\t%s\t%s\n",
                notification.request.identifier.UTF8String,
                notification.request.content.title.UTF8String,
                notification.request.content.body.UTF8String
            );
        }
        fflush(stdout);
        finish(0, nil);
    }];
}

static void printUsage(void)
{
    fprintf(
        stderr,
        "usage:\n"
        "  vicegerent-notifier authorize\n"
        "  vicegerent-notifier status\n"
        "  vicegerent-notifier post IDENTIFIER TITLE MESSAGE\n"
        "  vicegerent-notifier remove IDENTIFIER\n"
        "  vicegerent-notifier remove-all\n"
        "  vicegerent-notifier list\n"
        "  vicegerent-notifier version\n"
    );
}

int main(int argc, const char *argv[])
{
    @autoreleasepool {
        if (argc < 2) {
            printUsage();
            return 2;
        }

        NSString *command = [NSString stringWithUTF8String:argv[1]];
        if ([command isEqualToString:@"version"]) {
            finish(0, [NSString stringWithFormat:@"vicegerent-notifier %@", VicegerentNotifierVersion]);
        }

        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
        [NSApp finishLaunching];
        UNUserNotificationCenter *center = [UNUserNotificationCenter currentNotificationCenter];
        static VicegerentNotificationDelegate *delegate;
        delegate = [VicegerentNotificationDelegate new];
        center.delegate = delegate;

        if ([command isEqualToString:@"authorize"] && argc == 2) {
            requireAuthorization(center, ^{ finish(0, @"authorized"); });
        } else if ([command isEqualToString:@"status"] && argc == 2) {
            [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
                NSString *name = authorizationName(settings.authorizationStatus);
                BOOL available = (
                    settings.authorizationStatus == UNAuthorizationStatusAuthorized &&
                    settings.alertSetting == UNNotificationSettingEnabled &&
                    settings.alertStyle == UNAlertStyleAlert
                );
                NSString *message = name;
                if (
                    settings.authorizationStatus == UNAuthorizationStatusAuthorized &&
                    settings.alertSetting != UNNotificationSettingEnabled
                ) {
                    message = @"authorized, but alerts are disabled";
                } else if (
                    settings.authorizationStatus == UNAuthorizationStatusAuthorized &&
                    settings.alertStyle != UNAlertStyleAlert
                ) {
                    message = @"authorized, but Alert Style is not Persistent";
                }
                finish(
                    available ? 0 : 4,
                    message
                );
            }];
        } else if ([command isEqualToString:@"post"] && argc == 5) {
            postNotification(
                center,
                [NSString stringWithUTF8String:argv[2]],
                [NSString stringWithUTF8String:argv[3]],
                [NSString stringWithUTF8String:argv[4]]
            );
        } else if ([command isEqualToString:@"remove"] && argc == 3) {
            removeNotification(center, [NSString stringWithUTF8String:argv[2]]);
        } else if ([command isEqualToString:@"remove-all"] && argc == 2) {
            removeAllNotifications(center);
        } else if ([command isEqualToString:@"list"] && argc == 2) {
            listNotifications(center);
        } else {
            printUsage();
            return 2;
        }

        [NSApp run];
    }
    return 0;
}
