import logging
import boto.sns


class SnsHandler(logging.Handler):
    """Logs to the given Amazon SNS topic; meant for errors."""

    def __init__(self, arn, *args, **kwargs):
        super(SnsHandler, self).__init__(*args, **kwargs)
        self.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

        self.topic_arn = arn
        region_name = arn.split(':')[3]
        self.sns_connection = boto.sns.connect_to_region(region_name)

    def emit(self, record):
        subject = u'Production alert: {}: {}'.format(record.levelname, record.name)
        subject = subject.encode('ascii', errors='ignore')[:79]
        self.sns_connection.publish(
            self.topic_arn,
            self.format(record),
            subject=subject
        )
