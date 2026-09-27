from locust import HttpUser, constant, task


class FastUser(HttpUser):
    wait_time = constant(0)

    @task
    def index(self):
        self.client.get("/")
