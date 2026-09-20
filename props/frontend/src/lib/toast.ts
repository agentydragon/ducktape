import { notifications } from "@mantine/notifications";

export const toast = {
  success(message: string): void {
    notifications.show({ color: "green", message, autoClose: 8000 });
  },
  error(message: string): void {
    notifications.show({ color: "red", message, autoClose: 8000 });
  },
};
